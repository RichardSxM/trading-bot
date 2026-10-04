# 0005. Rationale: always on cloud deployment

## Context

Feature 7 moves the stack from the laptop to the London VPS and keeps it up without you. The scope's bar: the paper loop runs unattended for 72 hours, survives a forced restart, and a deploy never leaves an order unmanaged. Specs 0001, 0003, and 0004 already settle the stack (one Compose file, `restart: unless-stopped`, Tailscale only access, secrets as files in `/etc/kfs/secrets`, restic to Backblaze B2). They hand this feature four leftovers: script the secret placement, wrap the two file `docker compose` call, build the real backup job with its restore drill, and run the 0004 verify steps 1, 6, and 12 on real hardware.

The forces that shaped the choice:
- **The box is small.** OVHcloud VPS-1 in London: 2 vCore, 4 GB RAM, a 40 GB disk, plus a 50 GB extra disk you add. The memory limits from spec 0001 already add to about 3 GB. Anything that builds images or runs heavy jobs on the box competes with the process holding positions.
- **One operator, often away.** No on call rotation. Whatever breaks at 3am must heal itself or wait safely until you look. Simple tools you can read beat tools you must learn.
- **A restart is not free.** The trader holds positions and resting orders. NautilusTrader reloads its state from Redis and reconciles with Kraken, but that is feature 11 and not built yet. In demo, fills and stops are simulated by the sandbox execution client inside the trader, so a restart while positioned may lose the simulated state.
- **Code that exists today.** The trader publishes only a heartbeat snapshot and consumes no control command. The dashboard can already append to `{env}:control` and read `*:snapshot:*`. `control_commands.cmd` has a check constraint. Feature 6 (the strategy) does not exist yet, so the soak must wait for it.
- **Access is Tailscale only** (spec 0001), and the repo is private on GitHub.

## Options considered

### Option 1: provisioned VPS, CI images through GHCR, laptop deploy with pause and wait for flat (chosen)

An idempotent bootstrap script sets up the host. CI builds and pushes images per commit, and `scripts/deploy.sh` drives the VPS: pause entries, wait for flat, swap a release folder, gate on health, roll back automatically. Host systemd timers heal hung containers, and the backup container uploads nightly.

**Pros**:
- The image on the VPS is exactly the one CI tested. Nothing is built on the 4 GB box.
- Deploys are deliberate and never interrupt a position.
- Every piece is a short script you can read, and the laptop and VPS share the Compose files.

**Cons**:
- A registry token on the VPS and a few cents a month of GHCR storage.
- Many root scripts to maintain.
- A deploy can wait up to 15 minutes for flat.

### Option 2: git checkout and build on the VPS

The VPS keeps a clone (read only deploy key), and a deploy runs `git pull && docker compose build && up -d`.

**Pros**:
- Fewest moving parts: no registry, no CI changes, no token beyond the deploy key.

**Cons**:
- Building NautilusTrader images uses CPU, memory, and disk on the box that is trading.
- The image is not the one CI tested.
- The whole source tree and a repo key sit on the server.

### Option 3: automatic deploys (merge to main, or a Watchtower style poller)

CI joins the tailnet and deploys every green main commit, or the VPS polls GHCR and updates itself.

**Pros**:
- No manual step, so main is always what runs.

**Cons**:
- A merge restarts the trader at a moment nobody chose.
- CI needs a tailnet key, or the box updates itself without a pause.
- The paper record mixes in every merge.

### Option 4: a managed orchestrator or platform (Kubernetes, Nomad, a PaaS)

**Pros**:
- Rolling deploys, health based restarts, and secrets come built in.

**Cons**:
- A control plane that alone eats most of 4 GB, or a platform outside London and over the €20 budget.
- Far more to operate than one person needs, and spec 0001 already chose Compose.

## Rationale

The box is small and the operator is one person, so the deciding forces are "nothing heavy runs next to the trader" and "nothing restarts the trader at a bad moment". Option 1 is the only one that satisfies both. CI builds remove all build load from the VPS and make the deployed bits exactly the tested bits. The laptop command keeps you in charge of timing, and the pause plus wait for flat turns "never leave an order unmanaged" into a check the deploy makes, not a hope. Option 2 is simpler on paper but trades a registry token for builds that compete for 4 GB with the trader. Option 3 removes the human from the one decision that should have one. Option 4 is out on cost and operations alone.

Inside Option 1, these smaller calls were mine (each with its runner up):
- **Pause in two layers: a shared `KfsStrategy` base class plus NautilusTrader's `REDUCING` state.** `REDUCING` alone is not enough: it only denies an order that grows an existing position, so a flat trader could still open one (found by the cross check). The base class refuses every order that is not reduce only while paused, also when flat, and needs no cooperation from strategy code beyond subclassing it. `REDUCING` stays as the second layer. Runner up: `HALTED`, rejected because it also blocks the exits that let the trader reach flat.
- **`pause`/`unpause` as new commands** rather than reusing `stop`/`resume`. A deploy pause and a kill switch mean different things, and feature 9 may make stop flatten. Runner up: a plain Redis flag the deploy sets, rejected because it skips the acknowledged, recorded, expiring control path that already exists.
- **The deploy talks to Redis through a one off container of the new dashboard image** (`kfs-compose run --rm --no-deps dashboard python -m dashboard.control_cli`) rather than a new `deploy` Redis user. Using the new image means the deploy never depends on the old release having the CLI. The dashboard's ACL already allows exactly what is needed, so no new secret. Runner up: a `deploy` ACL user, rejected for one more secret and one more ACL line to keep right.
- **Auto rollback with expand only migrations** rather than leaving a failed release running. A broken trader that sits until you wake up is worse than the previous version. Expand only migrations are the price, and they are cheap at this schema's pace.
- **Host systemd timer for hung containers** rather than the common autoheal sidecar. The sidecar needs the Docker socket, which is root on the host. Runner up: an in process watchdog, rejected because it cannot see every way a process hangs.
- **`live-restore` plus Docker's repository excluded from unattended upgrades, needrestart in list mode.** Without them, a routine `apt` run can restart Docker and every container mid trade.
- **The extra disk holds only the catalog,** mounted with `nofail` and checked by a marker. If the disk goes missing, only the recorder stops. Moving Docker's whole data root there would make a missing mount start Postgres empty.
- **Append only B2 key on the VPS, prune from the laptop.** A compromised VPS cannot delete its own backups. The cost is that retention runs only when you run the monthly drill. Amended below: the VPS key can hide, not delete.
- **Tailscale SSH only, with the KVM console as the way back in.** It removes public port 22 and SSH key management entirely. The bootstrap refuses to close OpenSSH until a Tailscale login is confirmed, so you cannot lock yourself out in the same run.
- **Backup scheduling inside the backup container** (your pick) keeps the laptop and VPS identical, since the laptop has no systemd.

Your answers fixed the rest: Ubuntu LTS (more familiar than Debian 13; changed from 24.04 to 26.04 on 2026-10-03, see below), the bootstrap script over Ansible, a laptop triggered deploy, release bundles per commit, local logs only, 14 daily, 8 weekly, and 24 monthly retention, and a soak that allows self healing but counts it.

**Ubuntu 26.04 instead of 24.04 (amended 2026-10-03).** The VPS arrived with Ubuntu 26.04 LTS, and you chose to keep it rather than reinstall. 26.04 is supported to 2031, two years longer than 24.04. Docker's and Tailscale's apt repositories both publish `resolute` packages, chrony is already the default time service, and unattended-upgrades, needrestart, and `ssh.socket` work as on 24.04. Two defaults changed: `sudo` is sudo-rs, and most core utilities are the Rust coreutils (`cp`, `mv`, and `rm` stay GNU). I kept both defaults. The one sudoers rule uses a wildcard only in the command path, which sudo-rs supports, and the host scripts use only common coreutils flags (`readlink -f`, `install`, `stat -c`, `df -B1`, `mktemp -d`, `date -u`). Runner up: switching back to `sudo.ws` and GNU coreutils, rejected because it moves the box off the path Canonical tests, for no feature we need. If a script ever trips on a Rust coreutils difference, fix the script, or switch that one tool back to the GNU package.

**Hides, not deletes, on B2 (amended 2026-10-04).** The milestone 4 gate showed that restic 0.16.4 never hard deletes on B2: it writes a hide marker, which needs only `writeFiles`. So the VPS key can run `forget` after all, but every hidden file keeps its old version, and that key gets 401 on a real delete. Two forces pull against each other: keeping hidden versions is what makes a hide recoverable, and purging them is what lets the drill's prune free space. The bucket now purges a file 45 days after it was hidden. That outlasts the 35 day drill alert from feature 12 with margin, and the VPS key cannot change the rule because it lacks `writeBuckets`. You chose 45 days over keeping hidden files forever (safe, but storage never shrinks). Runner up: B2 Object Lock with a retention period, rejected because locked files make the drill's prune fail inside the window, and that is more moving parts than a one person bot needs. The other gate fallback, `rclone serve restic --append-only`, was not needed: lock removal works.

The cross check on this amendment added the drill guards. A VPS that can hide files and holds the restic password can make the drill restore an old snapshot and print `PASS`, or forge snapshots that push real ones out of retention, after which the drill's own prune would hide the real data. So the drill now fails on a newest snapshot older than 36 h (a night's slack over the 24 h schedule) and refuses a prune that removes recent or future dated snapshots, or more than 40 at once. Those limits sometimes refuse a harmless prune (a long gap between drills, two manual backups on one day); `--force-prune` after reading the dry run is the cheap way out. Runner up: an outside snapshot age check alone (feature 12), kept as well but not enough, because it does not stop the drill's prune. The recovery steps also gained a first step (revoke the VPS key and stop the backup), a defined time T, and the overwrite case, because `writeFiles` can upload a new version over an existing name.

## Evidence

**Memory budget on 4 GB** (limits from spec 0001, backup raised here): trader 1024 MB, recorder 768 MB, postgres 512 MB, redis 256 MB, dashboard 256 MB, backup 384 MB. That totals 3.2 GB of limits, against about 3.6 GB usable after Ubuntu, dockerd, containerd, and tailscaled (roughly 400 MB). Limits are ceilings, not usage. The 2 GB swap covers the nightly backup peak. The soak measures the real numbers.

**Disk budget, 40 GB system disk**: Ubuntu about 4 GB, swap 2 GB, three release image sets about 6 GB (five images that share base layers), container logs at most 0.7 GB (20 MB × 5 × 7 services), Postgres and Redis well under 2 GB for months, the backup staging dump under 1 GB. About 24 GB free stays as headroom; the deploy refuses under 5 GB. The catalog lives on the 50 GB extra disk.
