# 0005. Run the stack around the clock on the London VPS, deployed by one safe command

**Date**: 2026-10-03
**Status**: In Progress

## Summary

The bot moves from your laptop to the OVHcloud VPS in London and stays up by itself. One script sets up the server (Tailscale only access, firewall, swap, clock, Docker), and one command from your laptop deploys a tested build: it pauses new trades, waits until the bot holds no position, swaps in the new version, checks it is healthy, rolls back on its own if it is not, then resumes. Crashed or hung services restart automatically, a reboot brings everything back, and a nightly encrypted backup goes to Backblaze B2, with a monthly restore drill on your laptop. A 72 hour unattended run proves it once the paper loop (feature 6) exists.

## Requirements

**User stories**:
- As the trader, I want the bot and the recorder running 24/7 on a server near Kraken, so trading and data collection do not depend on my laptop being open.
- As the trader, I want to ship a new version with one command that never restarts the trader while it holds a position or a resting entry order, so a deploy can never leave an order unmanaged.
- As the trader, I want crashes, hangs, Docker restarts, and reboots to heal themselves, so the bot survives a night without me.
- As the trader, I want my fills, fees, and recorded market data backed up off the box and the restore proven, so losing the VPS never loses my records.
- As the trader, I want to see from my phone when the bot is paused and resume it there, so a deploy that dies halfway never leaves the bot idle without me knowing.

**Acceptance criteria**:
- **AC-1**: On a fresh Ubuntu 26.04 LTS VPS, `scripts/deploy.sh --bootstrap <public IP> [--data-disk <device>]` from the laptop streams the release bundle of `origin/main` over OVHcloud's public SSH into `/opt/kfs/releases/<sha>/` and runs `deploy/host/bootstrap.sh` from there. It ends with: Tailscale up as `tag:kfs` with Tailscale SSH on and the host name `kfs-vps`; OpenSSH stopped and disabled, but only after a Tailscale SSH login was confirmed in the same run; ufw denying all inbound except on `tailscale0` and UDP 41641, allowing all outbound; `ss -tulpn` showing no TCP listener on a public address and no UDP listener other than 41641 and the DHCP client (68); a 2 GB swapfile with `vm.swappiness=10`; time zone UTC with chrony synchronised (timesyncd disabled); Docker CE and the Compose plugin from Docker's apt repository with `live-restore: true`; unattended-upgrades for Ubuntu security origins only, with `Automatic-Reboot "false"`; needrestart in list only mode; when you pass `--data-disk` (in the first run or any later one), the extra disk mounted by UUID at `/srv/kfs` with `nofail` and the marker `/srv/kfs/catalog/.kfs-volume`; the `kfs` admin user, not in the `docker` group, with a passwordless sudoers rule for `/usr/local/sbin/kfs-*` only (`sudo -l -U kfs` lists exactly that rule); `/opt/kfs/current` pointing at the bootstrapped release. A second run changes nothing and says so.
- **AC-2**: `sudo /opt/kfs/current/deploy/host/place-secrets.sh` asks for each secret in the inventory that is missing from `/etc/kfs/secrets`, without echo, and writes it with owner uid 10001 and mode 0400 (`ghcr_token`: owner root, mode 0400) into the root only 0700 folder. It refuses an empty value or one containing whitespace, prints the secret's name and 8 hex fingerprint (never the value), and never overwrites an existing file unless you pass `--rotate <name>`. It then runs `docker login ghcr.io` with `ghcr_token` through stdin. When any of the three dashboard login files is missing, it pulls `kfs-dashboard:<current sha>` and runs `python -m dashboard.credentials --write` in it (no network, `/etc/kfs/secrets` bound at `/out`), then sets those files to owner 10001, mode 0400. After it, every secret the Compose files name exists, so the first deploy can start.
- **AC-3**: On a push to `main`, after the `check` job passes, CI pushes `ghcr.io/richardsxm/kfs-{core,trader,recorder,dashboard,backup}:<sha>`, each built with `GIT_COMMIT=<sha>`. A push to any other branch builds no image. A prune step keeps the newest 10 versions of each package.
- **AC-4**: `scripts/deploy.sh [sha]` on the laptop (default: `origin/main`) refuses a commit that is not on `origin/main`, and the VPS refuses it when any of the five images is missing (`docker manifest inspect` with the VPS's own GHCR login, so the laptop needs no package token). It uploads the release bundle to `/opt/kfs/releases/<sha>/`, pulls the images, switches `/opt/kfs/current`, and runs `up -d`. It passes when every service is healthy within 180 s and the trader heartbeat's `git_commit` equals `<sha>`. It prints one summary line and appends one JSON line to `/var/log/kfs/deploys.jsonl` with `sha`, `previous_sha`, `started_at`, `finished_at`, `outcome`, `paused_by_deploy`, `waited_for_flat_s`.
- **AC-5**: On the VPS every Compose call goes through `kfs-compose`. It always uses project name `kfs`, `/opt/kfs/current` as project folder, `/etc/kfs/kfs.env` as env file, `compose.yaml` plus `compose.vps.yaml`, and adds `compose.live.yaml` exactly when `TRADING_ENV=live`. `KFS_TAG` is the folder name `current` points to. After two deploys, `docker volume ls` shows one `kfs_pgdata` and one `kfs_redisdata`, and Postgres keeps its rows. On the laptop, `docker compose build` and `up` behave as before.
- **AC-6**: The control stream accepts `pause` and `unpause` with an optional `source` field (`dashboard` or `deploy`, absent means `dashboard`). The trader consumes `{env}:control` in group `trader`, writes `received` and then one final outcome to `{env}:control:results` within 2 s, records the command in `control_commands`, and handles a repeated `id` as `duplicate`. On `pause` it stores `{env}:trader:pause` (`source`, `paused_at`, `command_id`) and puts the trader in its paused state, which blocks entries in two layers. First, the shared strategy base class `KfsStrategy` refuses every order that is not reduce only before it reaches the engine, also when flat (logged `entry_denied_paused`). Second, the NautilusTrader risk engine is set to `REDUCING`, which also denies any order that would grow a position. Reduce only orders (exits, protective stops) still go through. On `unpause` it deletes the key and sets `ACTIVE`. A trader that starts while the key exists is paused before any strategy runs. Until feature 9, `stop`, `resume`, and `flatten` get the outcome `failed` with reason `not_implemented` and one WARNING. An expired command is acknowledged as `expired` and not executed.
- **AC-7**: Every 1 s the trader writes `{env}:snapshot:trader_state` (`EX 30`): `trading_state` (the risk engine's `ACTIVE`/`REDUCING`/`HALTED`, not the component state the heartbeat reports), `paused`, `pause_source`, `paused_at`, `open_positions`, `open_entry_orders` (open and in flight orders that are not reduce only), `protective_stops` (open reduce only stop orders), `git_commit`.
- **AC-8**: Every deploy recreates the trader (each commit is a new image), so every deploy first sends `pause` with `source=deploy`, unless the trader is already paused. It then waits until two fresh `trader_state` reads, 2 s apart, both show 0 open positions and 0 open entry orders. `--wait` sets the limit (default 15 minutes). On timeout it changes nothing, sends `unpause` if it paused, logs `outcome: aborted_not_flat`, and exits 2. If the pause disappears during the wait (you pressed Resume), it aborts the same way with `outcome: aborted_pause_lifted` and sends nothing. `--now` sends the pause but skips the wait. If the pause gets no `executed` result within 30 s, or `trader_state` is missing or older than 5 s (including a running release from before this feature, which publishes none), the deploy aborts the same way (fail closed) unless you passed `--now`. The deploy runs `control_cli` from the new release's dashboard image, so it never depends on the old release having it.
- **AC-9**: On SIGTERM the trader pauses entries (without writing the pause key), sends a cancel for every open entry order within 1 s, waits up to 10 s for the cancels to be acknowledged, never cancels a reduce only order and never closes a position, gives the recording actor up to 5 s to drain or spill its queue, logs `shutdown_graceful` with the counts of cancelled orders, unacknowledged cancels, positions kept, and stops kept, and exits 0 well within the 30 s stop grace period.
- **AC-10**: If the health gate fails, the deploy points `current` back at the previous release, starts its images, runs the gate again, logs `outcome: rolled_back` (or `rollback_failed`), and exits 1. Applied migrations stay applied; the rollback never runs a downgrade.
- **AC-11**: After a healthy deploy (or a healthy rollback), the deploy sends `unpause` only if this deploy sent the pause. A pause already present at the start, from you or a later kill switch, is left in place.
- **AC-12**: Only one deploy runs at a time: a second one exits 3 with `deploy in progress`. A deploy refuses to start, exit 4, when `/` or `/srv/kfs` has under 5 GB free, a secret file the Compose files name is missing, `ghcr.io` login fails, or a backup run is in progress (`/staging/running` exists in the backup container; deploy again after it logs `backup done`). After a successful deploy, only `current` and the two previous releases remain, in `/opt/kfs/releases/` and in the local images.
- **AC-13**: The dashboard top bar shows `paused · by deploy` or `paused · by you` from `trader_state`, plus `no data` when the snapshot is missing. The System page has Pause and Resume buttons. Each one sends `pause` or `unpause` with `source=dashboard` through the same CSRF and command state path as the stop button (sent, confirmed, not sent, expired). Resume clears any pause, whoever set it. An expired session on either POST gets the stop path's handling in `session.py` (`Pause NOT sent` / `Resume NOT sent` after login). The Activity feed lists pause and unpause rows next to stop rows.
- **AC-14**: `docker kill` of any long running service brings it back healthy within 2 minutes through the restart policy. Postgres and Redis data survive, and the trader comes back paused if it was paused.
- **AC-15**: A trader frozen from the host with `kill -STOP <its host PID>` turns unhealthy and is restarted by `kfs-autoheal` within 60 s after that, logging a JSON `autoheal_restart` line. The same happens for any `kfs` container. After 3 autoheal restarts of one container within an hour, it stops restarting that container, logs CRITICAL `autoheal_gave_up`, and keeps checking the others.
- **AC-16**: `systemctl restart docker` leaves every container running (same trader PID before and after), and every service is healthy afterwards.
- **AC-17**: After `reboot`, every service is healthy within 5 minutes, the dashboard answers on `https://kfs-vps.<tailnet>.ts.net`, and the catalog is on `/srv/kfs`. `sudo kfs-safe-reboot` pauses and waits for flat as a deploy does (same `--wait` and `--now`). It writes `/var/lib/kfs/resume-after-boot` only if it sent the pause, then reboots. After boot, `kfs-post-boot.service` waits for health, sends `unpause` only when that marker exists, and deletes it.
- **AC-18**: If the extra disk is not mounted at boot, the recorder refuses to start with CRITICAL `catalog_volume_missing` and writes nothing. Postgres, Redis, the trader, the dashboard, and the backup keep running.
- **AC-19**: Every night at `BACKUP_AT_UTC` (02:30), the backup service runs `SELECT downsample_snapshots()`, `pg_dump --format=custom` as `backup_app`, and writes a manifest (row counts of `orders`, `fills`, `funding_payments`, `control_commands`, and the newest fill time). It then runs `restic backup --host kfs-vps` (a fixed host name, so retention groups every night together) of the dump, the manifest, the catalog, `/etc/kfs/kfs.env`, and `/var/log/kfs/deploys.jsonl` to B2, never the secrets folder. It logs `backup done` with the restic snapshot id, bytes added, and the manifest, or ERROR `backup_failed` with the failed step and no retry until the next night. Its health check passes for `BACKUP_STALE_H` (26 h) after any container start, then only while the last success is younger than that, and never while the restic repository is missing.
- **AC-20**: The VPS uses only the B2 key without `deleteFiles` (`listBuckets`, `listFiles`, `readFiles`, `writeFiles`, this bucket only), so it can neither delete a file version nor change bucket settings. No retention or prune ever runs on the VPS. A hard delete of a file version with that key is refused. A `restic forget` with it is not refused: restic 0.16.4 never hard deletes on B2, it writes a hide marker (a newest version that makes the file look gone while the old versions stay). The bucket's only lifecycle rule purges a file 45 days after it was hidden, so anything hidden can be unhidden with the full key for 45 days.
- **AC-21**: `scripts/restore-drill.sh` on the laptop asks for the restic password and a delete capable B2 key without echo and never writes them to disk. It restores the newest snapshot into a throwaway `postgres:16` container, runs `pg_restore`, and compares row counts and the newest fill time with that snapshot's manifest. It prints `PASS` or `FAIL` with the differences and the snapshot's time and age, removes the container, then runs `restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 24 --prune` (only after a `PASS`). It reports `FAIL` and skips the prune when the newest snapshot is older than 36 h or dated in the future (`--allow-stale` accepts an old one). Before the prune it runs the same forget with `--dry-run` and refuses to prune when it would remove more than 40 snapshots, any snapshot younger than 15 days, or when any snapshot is dated more than 1 h in the future (`--force-prune` overrides after you read the list). `--self-test` deletes one `fills` row from the restored copy before comparing, so the drill must report `FAIL` naming `fills`, and skips the prune.
- **AC-22**: With `deploy/tailscale-policy.hujson` applied, only your user reaches `tag:kfs`, on tcp 443 and Tailscale SSH as `kfs`. A connection from `tag:kfs` to any other tailnet device fails. Another tailnet identity cannot reach the VPS.
- **AC-23**: The spec 0004 verify steps 1, 6, and 12 pass on the VPS: the dashboard login files written by `place-secrets.sh` (AC-2, which replaces spec 0004's `sudo scripts/dashboard-credentials.sh --write` plus `chown`/`chmod` on the VPS); the QR added to your authenticator app; the shell and the stop button checked on a real phone over Tailscale, including airplane mode.
- **AC-24** (after feature 6): `kfs-soak start` followed 72 h later by `kfs-soak report`, with no manual command in between, reports: every service healthy at the end, the longest `trader_state` gap under 2 minutes, every automatic restart listed (Docker `RestartCount` changes and `autoheal_restart` lines, allowed but counted), demo fills in Postgres equal to the `fill_seen` lines (spec 0006 AC-11, one per fill) `kfs-soak` counted while following the trader log live (rotation cannot lose them), and `protective_stops >= open_positions` in every sample, except a sample whose position is younger than spec 0006's `STOP_ACCEPT_TIMEOUT_S` or whose `trader_state` shows the exit loop running (amended 2026-10-04 by spec 0006: the stop goes on after the entry fills, and an exit may be closing a position whose stop failed). A deploy made during an open demo position waits for flat and leaves no order without its stop.

## Decision

**Chosen option**: Option 1: a provisioned VPS running the same Compose stack from CI built images, deployed by a laptop command that pauses the trader and waits for flat, healed by host timers, backed up by the backup container.

Build the VPS with one idempotent script, ship images built by CI through GHCR, and deploy with `scripts/deploy.sh`. The deploy pauses entries through a new `pause` control command, waits until the trader is flat, swaps a release folder, checks health, and rolls back automatically. A host systemd timer restarts hung containers, `live-restore` keeps containers alive through Docker restarts, and the backup container uploads a nightly restic snapshot to B2.

**Implementation skills**: `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `docker-patterns` (`affaan-m/ecc`, `.agents/skills/docker-patterns/`) · `github-actions-templates` (`wshobson/agents`, `.agents/skills/github-actions-templates/`) · `debian-linux-triage` (`github/awesome-copilot`, `.agents/skills/debian-linux-triage/`) · `tailscale` (`terminalskills/skills`, `.claude/skills/tailscale/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `htmx` (`damusix/skills`, `.claude/skills/htmx/`) · `fastapi` (`terminalskills/skills`, `.claude/skills/fastapi/`)

## Feature design

**The box.** OVHcloud VPS-1, London: 2 vCore, 4 GB RAM, 40 GB system disk, plus a 50 GB extra disk (it may be attached after the first bootstrap). Ubuntu 26.04 LTS (Resolute Raccoon, supported to 2031), kept with its own defaults: `sudo` is sudo-rs and most core utilities are the Rust coreutils. The host scripts use only what both support (see *Bootstrap steps*). `TRADING_ENV=demo` until feature 19.

**Repo layout added**:

| Path | What it is |
|---|---|
| `deploy/host/bootstrap.sh` | idempotent host setup (AC-1); each step checks before it changes, prints `ok` or `changed` |
| `deploy/host/place-secrets.sh` | interactive secret placement (AC-2) |
| `deploy/host/kfs-compose` | the only way to run Compose on the VPS (AC-5) |
| `deploy/host/kfs-deploy` | the VPS half of a deploy (AC-4, AC-8 to AC-12) |
| `deploy/host/kfs-autoheal` + `kfs-autoheal.service` + `kfs-autoheal.timer` | hung container restarts (AC-15) |
| `deploy/host/kfs-safe-reboot`, `kfs-post-boot.service` | planned reboots (AC-17) |
| `deploy/host/kfs-soak` | the 72 h soak sampler and report (AC-24) |
| `deploy/tailscale-policy.hujson` | the tailnet policy to paste into the admin console (AC-22) |
| `deploy/kfs.env.example` | the VPS env file template |
| `compose.vps.yaml` | VPS only override: catalog bind mount, recorder marker check, backup host mounts |
| `docker/backup/Dockerfile`, `docker/backup/run.sh` | the real backup job, replacing `stub.sh` (AC-19) |
| `scripts/deploy.sh` | the laptop half of a deploy (AC-4) |
| `scripts/restore-drill.sh` | the monthly restore drill (AC-21) |

**Host layout on the VPS**:

| Path | Owner, mode | Holds |
|---|---|---|
| `/etc/kfs/kfs.env` | root, 0644 | non secret settings (from `deploy/kfs.env.example`) |
| `/etc/kfs/secrets/` | root, 0700; files uid 10001 0400 (`ghcr_token` root 0400) | the spec 0003 inventory plus `ghcr_token` |
| `/opt/kfs/releases/<sha>/` | root, 0755 | one release bundle per deployed commit |
| `/opt/kfs/current` | symlink | the running release |
| `/usr/local/sbin/kfs-*` | symlinks into `/opt/kfs/current/deploy/host/` | host commands, updated by every deploy |
| `/var/log/kfs/deploys.jsonl` | root, 0644 | one line per deploy |
| `/var/lib/kfs/` | root, 0700 | `autoheal.json`, `resume-after-boot`, `soak/` |
| `/srv/kfs/catalog/` | extra disk, uid 10001 | the Parquet catalog and `.kfs-volume` |

**Bootstrap steps, in order** (each step is skipped when already done). `scripts/deploy.sh --bootstrap <public IP>` first streams `git archive origin/main` (the bundle paths listed under *Deploy sequence*) over `ssh root@<public IP>` (or the `ubuntu` user with sudo, as OVHcloud delivers it) into `/opt/kfs/releases/<sha>/`, then runs `deploy/host/bootstrap.sh --release <sha>` from there with a terminal attached:
1. Refuse unless Ubuntu 26.04 and root. Set the time zone to UTC and the host name to `kfs-vps`.
2. Create user `kfs` with a password you type twice (for the OVHcloud KVM console; keep it in the password manager). Add `kfs` to the `sudo` group (any command with its password) and add the sudoers file `/etc/sudoers.d/kfs`: `kfs ALL=(root) NOPASSWD: /usr/local/sbin/kfs-*`. sudo-rs accepts a wildcard in the command path (it refuses one only inside the arguments), so the rule stays as it is. The file is checked with `visudo -c` before it is moved into place, and the step then confirms the rule with `sudo -l -U kfs`.
3. apt: install chrony (already the default on 26.04; the step makes sure systemd-timesyncd stays off), ufw, unattended-upgrades, jq, and curl. Configure unattended-upgrades in its own file `/etc/apt/apt.conf.d/52kfs-unattended` (it loads after Ubuntu's `50unattended-upgrades` and clears its origins) with `Allowed-Origins` limited to `${distro_id}:${distro_codename}-security` and `Automatic-Reboot "false"`. Set needrestart to `$nrconf{restart} = 'l'`.
4. Swap: a 2 GB `/swapfile` in fstab, `vm.swappiness=10` in `/etc/sysctl.d/90-kfs.conf`.
5. Extra disk, optional: you pass the device (`--data-disk /dev/sdb`) in the first run or a later one. Without it the step is skipped, the catalog lives in `/srv/kfs` on the system disk, and no marker is written. When a later run adds the disk, the recorder is stopped, the existing `/srv/kfs` content is copied onto the disk, the old copy is kept beside it until you delete it, and the recorder starts again. An unformatted disk gets ext4 after you type `yes`. It is mounted by UUID at `/srv/kfs` with `defaults,nofail,x-systemd.device-timeout=30s`. Then `catalog/` (uid 10001) and the `.kfs-volume` marker are created. A disk that already has a file system is never formatted.
6. Docker CE, the CLI, containerd, and the Compose plugin from Docker's apt repository, for the codename in `/etc/os-release` (`resolute`). `/etc/docker/daemon.json` sets `{"live-restore": true, "log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "5"}}`. `kfs` is not added to the `docker` group (that would be root without sudo); Docker is used through `sudo kfs-compose` or `sudo docker`.
7. Tailscale from its apt repository, for the same codename. `tailscale up --ssh --advertise-tags=tag:kfs --hostname=kfs-vps`, with a one time auth key you paste at the prompt (never stored). Then `tailscale serve --bg --https=443 http://127.0.0.1:8000`.
8. Gate: print `tailscale ssh kfs@kfs-vps` and wait until you type `confirmed` after logging in that way from the laptop. Without it, the script stops here and leaves OpenSSH and the firewall untouched.
9. `systemctl disable --now ssh.socket ssh.service` (a unit that does not exist is skipped). ufw: default deny incoming, allow outgoing, `allow in on tailscale0`, `allow 41641/udp`, enable.
10. Create the folders from the host layout table and copy `deploy/kfs.env.example` to `/etc/kfs/kfs.env` if missing. Point `/opt/kfs/current` at the bootstrapped release, link `/usr/local/sbin/kfs-*` into it, and install and enable `kfs-autoheal.timer` and `kfs-post-boot.service` from it (every deploy refreshes the links and units).
11. Print the baseline: the median TCP connect time to `futures.kraken.com:443` over 20 tries, and `chronyc tracking` offset.

**Compose changes**:
- `compose.yaml`: every built image name becomes `${KFS_IMAGE_PREFIX:-kfs/}<app>:${KFS_TAG:-${IMAGE_TARGET:-runtime}}` (`<app>` is `core`, `trader`, `recorder`, `dashboard`, `backup`). The backup service gets a `build:` from `docker/backup/Dockerfile`, `mem_limit: 384m`, a `backup-staging` named volume at `/staging`, and a health check that runs `/usr/local/bin/kfs-backup health`. The recorder gets `CATALOG_REQUIRE_MARKER: ${CATALOG_REQUIRE_MARKER:-false}`. The laptop keeps the default project name (its existing volumes stay).
- `compose.vps.yaml`: the `catalog` mount in recorder, trader, and dashboard becomes `type: bind`, source `/srv/kfs/catalog`, with `bind: {create_host_path: true}` (read only for trader and dashboard, as today). The recorder gets `CATALOG_REQUIRE_MARKER: "true"`. The backup service gets read only binds of `/srv/kfs/catalog` at `/data/catalog`, `/etc/kfs/kfs.env` at `/host/kfs.env`, and `/var/log/kfs/deploys.jsonl` at `/host/deploys.jsonl`. No `pull_policy` is set; the deploy pulls explicitly and starts with `up --pull never`.
- `kfs-compose`: `set -eu`, run as root. `KFS_RELEASE` defaults to the folder name `current` points to; `kfs-deploy` sets it to the new release for commands that must use the new bundle and images before the switch (pull, the control CLI):
  `KFS_TAG=$KFS_RELEASE docker compose -p kfs --project-directory /opt/kfs/releases/$KFS_RELEASE --env-file /etc/kfs/kfs.env -f compose.yaml -f compose.vps.yaml [-f compose.live.yaml] "$@"`, adding the live file exactly when `/etc/kfs/kfs.env` sets `TRADING_ENV=live`.

**CI images job** (`.github/workflows/ci.yml`): job `images`, `needs: check`, `if: github.ref == 'refs/heads/main' && github.event_name == 'push'`, `permissions: {contents: read, packages: write}`. It logs in to `ghcr.io` with `GITHUB_TOKEN`, then runs `docker/build-push-action` per image with the matching `PACKAGE`/`APP_DIR`/`GIT_COMMIT=${{ github.sha }}` build args, `target: runtime`, `cache-from`/`cache-to: type=gha`, and pushes `ghcr.io/richardsxm/kfs-<app>:${{ github.sha }}`. No `latest` tag. A final step uses `actions/delete-package-versions` with `min-versions-to-keep: 10` per package.

**Deploy sequence** (`scripts/deploy.sh [--wait 15m] [--now] [sha]` on the laptop):
1. Resolve `sha` (default `git rev-parse origin/main` after `git fetch`). Refuse unless `git merge-base --is-ancestor <sha> origin/main`.
2. Stream `git archive <sha> compose.yaml compose.vps.yaml compose.live.yaml docker/redis docker/backup deploy` over `tailscale ssh kfs@kfs-vps sudo /usr/local/sbin/kfs-deploy receive <sha>`. The bundle unpacks into `/opt/kfs/releases/<sha>/` (an existing folder for that `sha` is replaced).
3. `kfs-deploy run <sha> [--wait] [--now]` on the VPS (run from the new bundle, `/opt/kfs/releases/<sha>/deploy/host/kfs-deploy`, so a changed deploy script takes effect at once), with output streamed back. Exit codes: 0 deployed, 1 rolled back or rollback failed, 2 aborted (not flat, pause lifted, or pause failed), 3 locked, 4 preflight refused.

`kfs-deploy run`, all under `flock -n /run/lock/kfs-deploy.lock`, with `KFS_RELEASE=<sha>` for steps 2 to 4:
1. Preflight (AC-12): free space; every secret the Compose files name exists; `docker login ghcr.io` still valid; `docker manifest inspect ghcr.io/richardsxm/kfs-<app>:<sha>` succeeds for all five images (AC-4); no `/staging/running` in the backup container.
2. `kfs-compose pull` (the old release keeps running).
3. Read `trader_state` with `kfs-compose run --rm --no-deps -T dashboard python -m dashboard.control_cli state` (new image, current Redis). If it is not paused, send `pause` (`source=deploy`, TTL 60 s) with `control_cli pause --source deploy --wait-result 30`. Unless `--now`, wait for flat (AC-8) with `control_cli wait-flat --timeout <wait>`.
4. Repoint `/opt/kfs/current` at the new release (atomic `ln -sfn` to a temp name plus `mv -T`), repoint `/usr/local/sbin/kfs-*`, reload systemd units if changed, then `kfs-compose up -d --no-build --pull never --remove-orphans`.
5. Health gate (AC-4): every service `healthy` (migrate `exited 0`), and trader `trader_state.git_commit == sha` and fresh, within 180 s.
6. Healthy: `unpause` if step 3 paused, prune releases and images to current plus two, append the deploy line, exit 0. Unhealthy: rollback (AC-10), then the same unpause rule after a healthy rollback (AC-11).

**Dashboard control CLI** (`apps/dashboard/src/dashboard/control_cli.py`, run as a one off container of the dashboard image with the dashboard's secrets and settings, so it uses the dashboard's own Redis login):
- `pause --source deploy --wait-result <s>`: appends the command (TTL 60 s), waits for its final result, exits 0 on `executed`, 1 otherwise.
- `unpause --source deploy --wait-result <s>`: same for `unpause`.
- `state`: prints `trader_state` as one JSON line, exits 1 if missing or older than 5 s.
- `wait-flat --timeout <dur>`: polls `trader_state` every 1 s, exits 0 after two reads 2 s apart both show 0 open positions and 0 open entry orders, exits 2 on timeout, exits 3 when `paused` turns false (pause lifted), exits 1 on a missing or stale snapshot (`--allow-stale` is not offered).

**Trader control path** (`apps/trader`, logic in `packages/core`):
- `trading_core.control.pause` (pure): `PauseState(source, paused_at, command_id)`, `encode`/`decode` for `{env}:trader:pause`, and `target_trading_state(paused: bool) -> TradingStateName` (`REDUCING` when paused, else `ACTIVE`). Feature 9 extends it with its stop state.
- `trading_core.control.messages`: `ControlCmd` gains `PAUSE = "pause"` and `UNPAUSE = "unpause"`. `ControlMessage` gains `source: ControlSource` (`dashboard`, `deploy`), written by `to_fields` and read by `from_fields` (missing means `dashboard`, an unknown value raises `ControlFormatError`). `pause_command(now, ttl_s, source)` and `unpause_command(...)` sit next to `stop_command`.
- `trading_core.node.strategy`: `PauseGate` (one per trader process, a thread safe holder of `paused`) and `KfsStrategy(Strategy)`, the base class every strategy subclasses. Its `submit_order` and `submit_order_list` refuse, while the gate is paused, any order that is not reduce only: the order is not submitted, and the strategy logs WARNING `entry_denied_paused` with the order's client id. Feature 6's strategies must subclass it and mark every exit and protective stop reduce only.
- `trading_core.node.control` (Nautilus side): a `ControlActor` registered before any strategy. `run_node` creates the `PauseGate`, passes it to the actor and every strategy, and hands the actor the node's risk engine (`node.kernel.risk_engine`). Pausing sets the gate and calls `set_trading_state(TradingState.REDUCING)`; unpausing does the reverse with `ACTIVE`. In `on_start` it reads the pause key and pauses when the key is present. A test asserts it starts before any strategy. It owns its own redis-py asyncio client (`socket_timeout` 3 s, above the 1 s blocking read; the snapshot writer's 0.2 s client stays separate) and an asyncio task that runs `XGROUP CREATE {env}:control trader 0 MKSTREAM` (ignoring `BUSYGROUP`), reads its own pending entries first, then `XREADGROUP GROUP trader trader BLOCK 1000 COUNT 10 STREAMS {env}:control >`. Per entry: decode (a bad entry gets `failed` plus WARNING `control_entry_invalid`), skip an `id` already recorded (`duplicate`), check expiry (`expired`), append `received`, execute, append the final outcome, record through `record_control_command`/`set_control_outcome`, and `XACK` last. A Postgres error logs ERROR `control_record_failed` and never blocks execution or the result. A Redis error logs `control_read_failed` at most once per 60 s and retries every 1 s; the trading state stays as it was.
- The snapshot writer task adds `trader_state` (AC-7) next to `heartbeat`: `trading_state` from `node.kernel.risk_engine.trading_state`, pause fields from the `PauseGate` and the pause key, `open_positions` from `cache.positions_open()`, and orders from `cache.orders_open()` plus `cache.orders_inflight()`, split by `is_reduce_only` and order type (`STOP_MARKET`, `STOP_LIMIT` reduce only count as protective stops).
- Shutdown (AC-9): `KfsStrategy.on_stop` pauses the gate (memory only; the key is not written, so an unplanned restart comes back unpaused unless you had paused) and calls `cancel_entry_orders()`, which cancels the strategy's open and in flight orders that are not reduce only. Strategies add nothing else that touches orders or positions on stop. The node config sets `timeout_post_stop` to 10 s so cancels can be acknowledged before the clients disconnect, and the ControlActor's `on_stop` logs `shutdown_graceful` with the counts (AC-9's budget: cancels sent within 1 s, acks up to 10 s, recording drain up to 5 s). Check the stop order of actors and strategies in 1.231 while building; the counts must be taken after the strategies stopped.

**Autoheal** (`kfs-autoheal`, oneshot, run every 30 s by its timer, `OnBootSec=2min`):
- List the containers with label `com.docker.compose.project=kfs` and `health=unhealthy`.
- For each one: read its restart times from `/var/lib/kfs/autoheal.json`, drop entries older than 1 h. Fewer than 3 left: `docker restart <name>` (it uses the container's stop timeout, 30 s for the trader), record the time, log `{"level":"WARNING","msg":"autoheal_restart","container":…,"restarts_last_hour":n}` to the journal. At 3: log CRITICAL `autoheal_gave_up` once per hour and leave it.
- Never touches a container in `starting` state, so the `start_period` of each health check is respected.

**Backup job** (`docker/backup/run.sh` in `ghcr.io/richardsxm/kfs-backup`, `alpine:3.20` plus `restic`, `postgresql16-client`, `jq`):
- Environment (from `compose.yaml`): `TRADING_ENV`, `KFS_TAG`, `RESTIC_REPOSITORY`, `BACKUP_AT_UTC`, `BACKUP_STALE_H`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER=backup_app`, `TZ=UTC`. `run.sh` sets `RESTIC_PASSWORD_FILE=/run/secrets/restic_password` and `PGPASSFILE` built in tmpfs from `/run/secrets/postgres_password`, and passes `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY`, read from their files, only in the environment of each `restic` call.
- Startup: the existing secret checks and fingerprints (spec 0003 AC-12), then `restic cat config` to check the repository. If it does not exist yet, log `restic_repo_missing` and keep running unhealthy; the repository is created once by hand (`verify.md`).
- Loop: sleep until the next `BACKUP_AT_UTC`, create `/staging/running`, run the steps in order, and remove it at the end, success or not (a stale one from a killed run is removed at startup). Any failure logs `backup_failed` with `step` (`downsample`, `dump`, `manifest`, `upload`) and skips the rest until the next night.
- Writes `/staging/last_success` (UTC epoch) after a clean run. Startup writes `/run/kfs/started`. `kfs-backup health` exits 0 while `/run/kfs/started` is younger than `BACKUP_STALE_H` (26 h), a grace after every start, not just the first; after that, only while `last_success` is younger than 26 h. It exits 1 whenever the repository is missing (`/run/kfs/repo_missing`, set by a failed `restic cat config` at startup or at the start of a run, cleared by a successful one or by a successful upload). The grace means a container that restarts more often than every 26 h stays healthy even if every backup fails; the `backup_failed` log line and the outside snapshot age alert (feature 12) catch that case, not this health check.
- Restic tags: `kfs`, `env:<TRADING_ENV>`, `sha:<KFS_TAG>`. The manifest file is `manifest.json` inside the snapshot.

**B2 bucket** (one private bucket, repository under the `kfs/` prefix, no Object Lock):
- Keys: `kfs-vps-append` on the VPS (`listBuckets`, `listFiles`, `readFiles`, `writeFiles`) and `kfs-laptop-full` in the password manager (those plus `readBuckets`, `deleteFiles`), both limited to this bucket. Neither has `writeBuckets`, so neither can change the lifecycle rule; only the account owner can.
- Lifecycle: one rule, prefix empty, `daysFromHidingToDeleting` 45, `daysFromUploadingToHiding` unset (in the web UI: keep prior versions for 45 days). Restic's deletes are hides, so the drill's prune frees space 45 days after it runs, and a hide by a compromised VPS can be undone for 45 days. The same rule purges restic's hidden lock files, and any older version of a file that was uploaded again.
- Recovering after a suspected VPS compromise, all with the full key in the B2 CLI:
  0. Revoke `kfs-vps-append` (`b2 key delete <key id>`) and stop the backup container, so nothing changes while you work. Make a new VPS key afterwards and place it with `place-secrets.sh --rotate`.
  1. Pick the time T: the start of the last drill that passed before the suspected compromise (or of the earliest backup you still trust). Everything the VPS did after T is suspect. Unhiding more than needed is safe: it only brings back snapshots an earlier prune had forgotten, and the next drill forgets them again.
  2. List every version: `b2 ls --json --versions --recursive b2://<bucket>/kfs/ > versions.json`. Each line has `fileName`, `fileId`, `action` (`upload` or `hide`), and `uploadTimestamp` (ms).
  3. Unhide every file outside `kfs/locks/` (so `config`, `keys/`, `snapshots/`, `index/`, and `data/` alike) whose newest version is a hide made after T: `jq -r --argjson t <T in ms> 'group_by(.fileName)[] | max_by(.uploadTimestamp) | select(.action == "hide" and .uploadTimestamp > $t and (.fileName | startswith("kfs/locks/") | not)) | .fileName' versions.json | while read -r f; do b2 file unhide "b2://<bucket>/$f"; done`.
  4. The VPS key can also upload a new version over an existing name (restic never does this itself, every file name is its content's hash). For every file outside `kfs/locks/` with more than one `upload` version, delete the uploads made after T: `b2 rm b2id://<fileId>`.
  5. `restic check --read-data` from the laptop, then a normal drill.

  Unhiding a hidden snapshot was proven on 2026-10-04; the recovery of a VPS `forget --prune` is verify step 18.

**Data model sketch**: one migration, `0003_pause_commands`: replace the check constraint on `control_commands.cmd` to allow `stop`, `resume`, `flatten`, `pause`, `unpause`. The downgrade restores the three value check (it fails if pause rows exist, which is correct). No new table, no new column: `source` lives in the stream entry and the Redis pause key, not in Postgres. New Redis keys: `{env}:trader:pause` (trader writes, no TTL, AOF persisted), `{env}:snapshot:trader_state` (trader writes, `EX 30`, dashboard reads under its existing `%R~*:snapshot:*` grant).

**State transitions** (trader pause state, as this feature drives it):
- running → paused (gate set, `REDUCING`, key written) on `pause`, or at startup when the key exists.
- paused → running (gate cleared, `ACTIVE`, key deleted) on `unpause`, never on its own.
- running → stopping (gate set in memory only, entries cancelled) on SIGTERM; the next start reads the key as usual.
- Deploy: `idle` → `preflight` → `pulled` → `pausing` → `waiting_flat` (skipped with `--now`) → `switched` → `gating` → `done` | `rolling_back` → `rolled_back` | `rollback_failed`; `aborted_not_flat` or `aborted_pause_lifted` from `pausing` or `waiting_flat`.

**API surface**:

| Surface | Method / call | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/control/pause` (dashboard) | POST | CSRF token | the command state fragment, as stop | session cookie | 403 CSRF, 503 Redis down (`Pause NOT sent`) |
| `/control/unpause` (dashboard) | POST | CSRF token | the command state fragment | session cookie | 403, 503 |
| `{env}:control` stream | XADD | `id`, `cmd`, `issued_at`, `expires_at`, `schema_version`, `source` | entry id | Redis ACL `dashboard` | `NOPERM` for any other user write |
| `{env}:control:results` stream | XADD (trader) | `id`, `outcome`, `outcome_at`, `schema_version`, `reason` (on `failed`) | entry id | Redis ACL `trader` | none |
| `scripts/deploy.sh` | CLI | `sha` (opt), `--wait`, `--now` | summary line, exit code | your tailnet identity | exit 1 to 4 as above |
| `kfs-deploy receive/run` | CLI on VPS | `sha`, flags | JSON log lines, exit code | sudo (`kfs`) | as above |
| `kfs-safe-reboot` | CLI on VPS | `--wait`, `--now` | reboot | sudo | exit 2 not flat |
| `dashboard.control_cli` | CLI in container | subcommand, flags | JSON line, exit code | dashboard Redis login | 1 stale or failed, 2 timeout |
| `kfs-soak` | CLI on VPS | `start` / `report` | `/var/lib/kfs/soak/<start>.jsonl`, report text | sudo | 1 no running soak |
| `scripts/restore-drill.sh` | CLI on laptop | prompts, `--self-test`, `--allow-stale`, `--force-prune` | `PASS`/`FAIL`, snapshot age | restic password, B2 key typed in | 1 on FAIL, stale snapshot, or refused prune |

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| deploy | target `sha` | CLI argument, else `origin/main` after `git fetch` |
| deploy | image reference | `KFS_IMAGE_PREFIX` in `/etc/kfs/kfs.env` (`ghcr.io/richardsxm/kfs-`) plus `<app>:<sha>` |
| deploy | `previous_sha` | `readlink -f /opt/kfs/current` before the switch |
| deploy | drain needed | always (every commit is a new trader image) |
| deploy | backup running | `/staging/running` in the backup container |
| deploy | flat | `trader_state.open_positions`, `open_entry_orders`, `ts_ns` (AC-7) |
| deploy | already paused | `trader_state.paused` read before sending `pause` |
| deploy | wait limit | `--wait`, default 15 minutes |
| deploy | health | `docker inspect` health status per service; `trader_state.git_commit` vs `sha` |
| deploy | `waited_for_flat_s` | wall time from pause result to the second flat read |
| `kfs-compose` | `KFS_TAG` | folder name `current` points to |
| `kfs-compose` | live override on/off | `TRADING_ENV` in `/etc/kfs/kfs.env` |
| pause | `source` | stream field from the sender (dashboard route sets `dashboard`, CLI flag sets `deploy`) |
| pause | `paused_at` | trader's UTC clock when executing |
| top bar | `paused · by …` | `trader_state.paused`, `pause_source` |
| `trader_state` | counts | NautilusTrader cache in the trader |
| `trader_state` | `git_commit` | `Settings.git_commit` (image build arg) |
| autoheal | restarts in the last hour | `/var/lib/kfs/autoheal.json` |
| backup | run time | `BACKUP_AT_UTC` |
| backup | manifest counts, newest fill time | one `REPEATABLE READ` session as `backup_app` runs `pg_export_snapshot()` and takes the counts, then `pg_dump --snapshot=<id>` dumps exactly that state while the session stays open |
| backup | restic repository | `RESTIC_REPOSITORY` in `/etc/kfs/kfs.env` |
| backup | stale limit | `BACKUP_STALE_H`, default 26 |
| restore drill | retention | fixed in the script: 14 daily, 8 weekly, 24 monthly |
| restore drill | stale limit, prune guard | fixed in the script: newest snapshot under 36 h; dry run removes at most 40, none younger than 15 days, none dated over 1 h ahead |
| restore drill | snapshot age | the newest `kfs-vps` snapshot's own `time` against the laptop's UTC clock |
| soak | gaps, restarts, fills | `trader_state` samples every 10 s; `docker inspect .RestartCount`; journal `autoheal_restart`; `fills` rows vs `OrderFilled` log lines in the window |

**Key invariants**:
- The trader is never recreated by a deploy while `open_positions > 0` or `open_entry_orders > 0`, unless you passed `--now`.
- A deploy only ever sends `unpause` for a pause it sent itself.
- Nothing in this feature cancels a reduce only order or closes a position.
- On the VPS, Compose never runs with a project name other than `kfs`, and never with `compose.live.yaml` unless `TRADING_ENV=live`.
- Migrations are expand only: a release's schema must still work with the previous release's code, because rollback never downgrades.
- No secret appears in a release bundle, an image, a log line, a backup, or a process argument; `ghcr_token` reaches `docker login` through stdin.
- No container gets the Docker socket.
- The VPS never hard deletes from B2; whatever it hides stays recoverable for 45 days.

**Security model**:
- Network: no public listener except Tailscale's UDP port. The admin shell is Tailscale SSH as `kfs`, and only your identity may use it. The OVHcloud KVM console with the `kfs` password is the way in when Tailscale breaks.
- Privilege: `kfs` runs only `kfs-*` commands as root without a password; anything else (including `docker`) needs `sudo` with the `kfs` password. `kfs` is not in the `docker` group.
- Registry: `ghcr_token` is a classic PAT with only `read:packages`, expiring within 1 year. The images contain no secret.
- Tailnet: `tag:kfs` can initiate nothing. This contains a compromised VPS.
- Backups: restic encrypts on the VPS before upload. The VPS key cannot delete a file version or change the lifecycle rule, so a compromised VPS cannot destroy its own backups. It can hide snapshots with `restic forget`, upload new versions over existing files, and, since it holds the restic password, add forged snapshots. You then have 45 days to notice and recover (see *B2 bucket*). The monthly drill notices: it fails when the newest snapshot is over 36 h old, and refuses a prune that would push recent or future dated snapshots out of retention. The delete capable key exists only in the password manager and is typed in for the drill.
- Control: pause and unpause go through the dashboard login (password plus TOTP, CSRF) or the VPS shell. Both are inside Tailscale. A pause can only reduce risk; an unpause returns to the state before it.

**Configuration required** (non secret, `/etc/kfs/kfs.env` on the VPS, `.env` on the laptop):
- `KFS_IMAGE_PREFIX`: image name prefix. Laptop default `kfs/`; VPS `ghcr.io/richardsxm/kfs-`.
- `KFS_TAG`: set by `kfs-compose` and `kfs-deploy`, never by hand.
- `CATALOG_REQUIRE_MARKER`: recorder refuses to start without `/data/catalog/.kfs-volume` when true (VPS true, laptop false).
- `RESTIC_REPOSITORY`: `b2:<bucket>:kfs`.
- `BACKUP_AT_UTC`: daily backup time, `HH:MM`, default `02:30`.
- `BACKUP_STALE_H`: health limit in hours, default 26.
- `KFS_HOST` (laptop only, for `deploy.sh`): tailnet host name, default `kfs-vps`.
- New secret: `ghcr_token` (VPS host only, used by `docker login`, mounted into no container). Add it to the spec 0003 inventory in `verify.md`.
- Prerequisites outside code: the extra 50 GB disk attached in the OVHcloud panel; Tailscale MagicDNS and HTTPS certificates enabled; the tailnet policy pasted; a GHCR classic PAT; the restic repository initialised once; the bucket lifecycle rule above; a second, delete capable B2 key kept in the password manager.

**Critical test scenarios**:
- Happy path: deploy a new main commit while flat; images pulled, trader drained and replaced, healthy, resumed, one deploy line, verifies **AC-4**, **AC-8**, **AC-11**.
- Deploy while not flat: timeout aborts with nothing changed and the trader resumed, verifies **AC-8**.
- Bad release (a trader image that exits at boot): automatic rollback, previous release healthy, migrations untouched, verifies **AC-10**.
- Hung trader (`kill -STOP` on its host PID): autoheal restarts it; four freezes within an hour end in `autoheal_gave_up`, verifies **AC-15**.
- Missing extra disk: only the recorder refuses, verifies **AC-18**.
- Unit tests in core: pause key encode and decode, `target_trading_state`, `source` parsing and its unknown value error, `pause_command` and `unpause_command` fields, verifies **AC-6**.
- Trader tests: a paused trader denies a new entry and accepts a reduce only stop; a duplicate `id` gives `duplicate`; an expired command gives `expired`; a restart with the key present starts `REDUCING`; `stop` gives `failed`/`not_implemented`, verifies **AC-6**, **AC-9**.
- Dashboard tests: Pause and Resume follow the stop button's CSRF and Redis down paths; the top bar shows the pause source; the ACL still denies `SET`, verifies **AC-13**.
- Auth/permission: another tailnet identity cannot reach port 443 or SSH; the dashboard user cannot write `{env}:trader:pause`; the VPS B2 key cannot hard delete, and a hide it makes is undone with the full key, verifies **AC-20**, **AC-22**.

## Build plan

Tracer Bullet: the first milestone puts the real stack on the real box through the real deploy path, thin. Each later milestone thickens one segment end to end.

**Milestone 1: thin thread to the VPS**
1. `compose.yaml` image names with `KFS_IMAGE_PREFIX`/`KFS_TAG`; `compose.vps.yaml`; `deploy/kfs.env.example`; `deploy/host/kfs-compose`. Check on the laptop that `docker compose config` is unchanged apart from the image names, satisfies **AC-5**.
2. CI `images` job and pruning (the backup image arrives in milestone 4, so this job builds four images until then), satisfies **AC-3**.
3. `deploy/host/bootstrap.sh` with every step in order and the SSH gate, plus the `--bootstrap` mode of `scripts/deploy.sh` that streams the bundle and runs it, run on the real VPS, satisfies **AC-1**.
4. `deploy/host/place-secrets.sh` (including GHCR login and the dashboard login files from the pulled dashboard image) and the `ghcr_token` inventory row, satisfies **AC-2**.
5. `scripts/deploy.sh` and `kfs-deploy receive/run` without the drain (preflight, pull, switch, up, health gate on Docker health plus heartbeat `git_commit`, deploy log), satisfies **AC-4**, **AC-12**.
6. `deploy/tailscale-policy.hujson`, applied, and `tailscale serve`; the 0004 carried steps on the VPS and a phone, satisfies **AC-22**, **AC-23**.

**Milestone 2: safe deploy**
7. `ControlCmd.PAUSE`/`UNPAUSE`, `ControlSource`, `pause_command`/`unpause_command`, `trading_core.control.pause`, migration `0003_pause_commands`, with core unit tests, satisfies **AC-6**.
8. `PauseGate` and `KfsStrategy`, the trader `ControlActor` (consumer, pause key, gate plus risk engine state, results, records, `not_implemented` path), and the `trader_state` snapshot, with a test that a flat paused trader refuses an entry, satisfies **AC-6**, **AC-7**.
9. Shutdown helper and the ControlActor `on_stop`, with the graceful shutdown log, satisfies **AC-9**.
10. `dashboard.control_cli`, then the drain in `kfs-deploy` (pause through the new image, wait flat, timeout and pause lifted aborts, the old release refusal, resume rule) and the backup running check, satisfies **AC-8**, **AC-11**, **AC-12**.
11. Rollback in `kfs-deploy`, release and image pruning, the flock, satisfies **AC-10**, **AC-12**.
12. Dashboard Pause and Resume routes and buttons on System, the top bar pause text, Activity rows, satisfies **AC-13**.

**Milestone 3: self healing**
13. `kfs-autoheal` with its unit and timer, satisfies **AC-15**.
14. The recorder marker check (`catalog_volume_missing`), satisfies **AC-18**.
15. `kfs-safe-reboot` and `kfs-post-boot.service`, satisfies **AC-17**.
16. Run the restart drills on the VPS (kill each service, freeze the trader, restart Docker, reboot, unmount the extra disk), satisfies **AC-14**, **AC-15**, **AC-16**, **AC-17**, **AC-18**.

**Milestone 4: backups**
17. Gate first: `restic init` with the full key from the laptop, then two `restic backup` runs with the key that lacks `deleteFiles`, and check that they finish without stale lock errors (see Follow-up for the fallback). Then check what `forget` and a hard delete do with that key, set the bucket lifecycle rule, and prove an unhide, satisfies **AC-20**.
18. `docker/backup/Dockerfile` and `run.sh` (loop, downsample, dump, manifest, upload, health), the backup image added to CI, satisfies **AC-19**, **AC-3**.
19. `scripts/restore-drill.sh` with the freshness check, the prune dry run guard, and the prune, run once for real, satisfies **AC-21**, **AC-20**.

**Milestone 5: soak (after feature 6 is built)**
20. `kfs-soak start|report`, satisfies **AC-24**.
21. With the feature 6 strategy live in demo: deploy during an open position (waits, then resumes), then a 72 h run with no manual command, satisfies **AC-8**, **AC-9**, **AC-24**.

## Consequences

**Positive**:
- The laptop and the VPS run the same Compose files and the same images, so a test on the laptop covers what runs on the box.
- Every deployed image is the exact build CI tested, tagged by its commit, which spec 0002 already records on every strategy version.
- A deploy cannot strand a resting entry order or interrupt a held position, and a failed release rolls itself back.
- Pause is enforced in two layers, the shared `KfsStrategy` base and NautilusTrader's risk engine, so one slip in a strategy does not open a position during a deploy.
- Restarts, hangs, Docker upgrades, and reboots heal without you; backups cannot be destroyed from the VPS.

**Negative / tradeoffs**:
- A deploy can wait up to 15 minutes, and a busy scalper may never be flat. `--now` exists, but then the restart relies on exchange side stops and feature 11.
- Expand only migrations make a rename or a drop a two release change.
- Many small bash scripts with root reach. They need careful review, and a `bash` error on the box shows up at deploy time, not in CI, until the `shellcheck` step from Follow-up is in place.
- In demo, the sandbox execution client's simulated position and stops may not survive an unplanned restart (crash, reboot). The deploy avoids it by waiting for flat; feature 11 owns the rest.
- 4 GB RAM is tight: the long running limits add to 3.2 GB plus about 0.4 GB for the OS, and swap absorbs peaks such as the nightly backup.
- GHCR private storage goes past the free 500 MB; expect cents per month.
- Passwordless `kfs-*` sudo means anyone with your Tailscale identity can deploy or reboot; Tailscale identity is the real lock.
- Every strategy must subclass `KfsStrategy` and mark exits reduce only. A strategy that bypasses it is only held back by `REDUCING`, which lets a flat trader open a position.
- Until feature 9, the dashboard's stop button now gets a `failed` answer (`not_implemented`) instead of no answer, so it shows "Stop failed". That is the truth, but it looks alarming.
- Retention only runs when you run the monthly drill, and its prune frees space only 45 days later (restic hides, the lifecycle rule purges). If you skip drills, the B2 repository grows (slowly; it costs cents).
- A compromised VPS can still make every snapshot vanish from restic's view. Nothing is lost for 45 days, but only you notice it, through the drill or the feature 12 alerts. Skip drills and miss alerts for 45 days, and the hidden backups are gone. The margin over the 35 day drill alert is 10 days; raise the lifecycle rule if you expect to skip drills.
- The backup health check gives a 26 h grace after every container start, so frequent restarts can hide failing backups from it; the outside snapshot age alert covers that.
- The drill's prune guard refuses after a long gap without drills (more than 40 snapshots to forget) or after manual extra backups on one day in the last 15 days; you read the dry run and pass `--force-prune`.

**Neutral**:
- The laptop stack keeps its own project name and volumes; its demo record and the VPS demo record diverge. The VPS is the paper record that counts.
- The `backup` stub is replaced by a real image, so the laptop now builds one more image.
- `unattended-upgrades` excludes Docker's repository, so Docker upgrades are a manual `apt upgrade` followed by `kfs-safe-reboot` if the kernel changed.

## Follow-up

- [x] **Gate (milestone 4), 2026-10-04:** `restic backup` with the key without `deleteFiles` removes its locks fine (restic hides them). The surprise was that `forget` with that key succeeds as hides. AC-20, the bucket lifecycle rule, and the recovery steps were amended to match; Object Lock was not added (it fights the drill's prune for a one person bot).
- [ ] If restic is upgraded (an Alpine bump), rerun verify step 18. A restic that hard deletes on B2 makes `forget` on the VPS fail outright (stronger) and the drill's prune free space at once; a change in lock handling could reopen the gate.
- [ ] Feature 6's spec adopts this contract: every strategy subclasses `KfsStrategy` (pause gate, `on_stop` cancels entry orders and nothing else); every exit and protective stop is reduce only; the recording actor drains or spills on stop within 5 s.
- [ ] Feature 9 builds `stop`, `resume`, and `flatten` on the ControlActor from this spec and decides how stop and pause combine in `target_trading_state`. It also rewords spec 0004 verify step 21, since stop now answers `failed`/`not_implemented` until then.
- [ ] Feature 11: test that a crash with an open demo position rebuilds the sandbox position and its stop, or define what happens if it cannot.
- [ ] Feature 12: alert on `autoheal_gave_up`, `backup_failed`, an unhealthy backup, a deploy `rollback_failed`, disk under 5 GB, a restore drill older than 35 days, and a newest `kfs-vps` snapshot in B2 older than 26 h (seen from outside the VPS, since a compromised VPS can hide snapshots and still report healthy).
- [ ] Feature 8: local catalog retention on the 50 GB disk (B2 holds the history), and which catalog paths the backup should skip while an hour is still open.
- [ ] Spec 0001: amend the Hosting row (2 vCore, 4 GB, 40 GB plus a 50 GB extra disk, Ubuntu 26.04) and the Backups row (append only VPS key, prune from the laptop). Spec 0003: add `ghcr_token` to the inventory and mark its two Follow-ups (secret placement, two file command) done by this spec.
- [ ] Watch `docker stats` during the soak. If the trader gets near 1 GB or swap use stays high, revisit the memory limits or the plan size.
- [ ] Add `shellcheck` to pre-commit and CI for `deploy/` and `scripts/` (pin it like ruff).
- [ ] `/sync`: add `deploy/host/` and the VPS commands (`kfs-compose`, `kfs-deploy`, `scripts/deploy.sh`, `kfs-safe-reboot`) to root `AGENTS.md` Commands, and add `docker-patterns`, `github-actions-templates`, and `debian-linux-triage` under `## Agent skills`. Record that no Agent Skill was found for restic or systemd, and that `julianobarbosa/claude-code-skills@shellcheck` was declined (it tells the agent to POST to a local notification server).

## Rationale

Reasoning and options: see [rationale.md](rationale.md). Verify steps: see [verify.md](verify.md).
