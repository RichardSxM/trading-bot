# Review, feat/test-host-scripts (spec 0005 files, already on main), 2026-10-17

**Reviewed by**: Sonnet 5.5 (author on an earlier model)
**Scope**: about 40 files, current content (MODE=files, BASE=main)
**Verdict**: Changes requested

## Summary
The deploy path is carefully built: preflight and image pull happen before anything is paused, the drain fails closed, rollback exists, pauses are lifted only when the deploy sent them, and secrets stay out of ps and logs. Three problems can leave the trader paused or lose an operator command: a control command lost as `duplicate` across a restart, a deploy script with no guard against its SSH session dying mid-run, and an unconditional unpause that can lift an operator's own pause. The backup and restore drill leave the catalog snapshots, the only copy of old catalog hours, unverified.

## Major
### 🟠 A stop or pause recorded as received but not executed comes back as `duplicate`, `packages/core/src/trading_core/control/consumer.py:175`
**Problem**: `_new` writes `received` to Postgres, then executes. `known()` is true for any row, whatever its state. If the process is cancelled or killed between those two steps (`ControlActor.on_stop` cancels the task, so a deploy or autoheal restart can do it), the unacked entry is re-read at the next start. `known` is now true, so the entry is answered `duplicate` and acked without running.
**Why it matters**: A dashboard Stop (kill switch) pressed while the trader is restarting can be dropped and shown as `duplicate`. A deploy's `pause` has the same hole, though the deploy only sees a non-executed result and aborts.
**Suggested fix**: Treat a known command whose recorded outcome is still `received` as unfinished and run it. Make `known` return the outcome, or add a query for "finished". Add a test for a crash between `received` and `execute`.

### 🟠 A deploy killed by a dropped SSH session leaves the trader paused or half switched, `deploy/host/kfs-deploy:235`
**Problem**: `run` has no `trap` for HUP, PIPE, TERM or EXIT, and `deploy.sh` runs it through `tailscale ssh` without a terminal. The drain can last 15 minutes. If the laptop sleeps or the tailnet blips, sshd closes the channel. The next `kfs_log` write to stdout raises SIGPIPE and bash dies. That can happen after the pause was sent, or after `switch_release` but before `up -d` and the health gate. The result is no rollback, no unpause, `current` pointing at an unstarted release, and a released lock.
**Why it matters**: The trader stays paused with no alert that says why. If it dies after the switch, `current` and the running containers disagree, and the next autoheal or safe-reboot acts on the wrong release.
**Suggested fix**: Ignore HUP and PIPE in `run`, or re-exec under `systemd-run` or `setsid` with output going to the log file. Add an EXIT trap that, when the switch has happened and no outcome is recorded, rolls back, or at least records an outcome and lifts the deploy's own pause.

### 🟠 `kfs_unpause_if_ours` lifts a pause that is no longer the deploy's, `deploy/host/lib.sh:152`
**Problem**: The check is the local flag `KFS_PAUSED_BY_US`. The trader's `_pause` overwrites the key, including its `source`, so if you press Pause on the dashboard during the 15 minute drain, the pause becomes `source=dashboard`. After the deploy the script still sends `unpause`, which has no source check, and the trader resumes. `kfs-safe-reboot post-boot` does check `pause_source == deploy`; the deploy path does not.
**Why it matters**: AC-11 says only a pause the deploy sent is lifted. This breaks that, and the trader trades after an operator explicitly paused it.
**Suggested fix**: Before sending `unpause`, read `trader_state` and send it only when `paused` is true and `pause_source` is `deploy`. Apply the same check in the rollback path.

### 🟠 The restore drill never checks or restores the catalog snapshots, `scripts/restore-drill.sh:219`
**Problem**: The drill restores only the newest non-catalog snapshot and compares four row counts. It never restores a `catalog` snapshot and never runs `restic check`, yet the prune right after it keeps `--keep-tag catalog` forever. Per the script's own header and spec 0007, the compactor deletes local catalog files once they are in a catalog snapshot.
**Why it matters**: A corrupt, partial or unreadable catalog snapshot would pass every drill while the local copy is already gone. The loss shows up only when you need the data.
**Suggested fix**: Add `restic check` (`--read-data-subset` is enough) before the prune. Restore the newest catalog snapshot, or a sample of it, and check that its manifests and Parquet files read.

## Minor
### 🟡 Autoheal drops the deploy lock before it acts, `deploy/host/kfs-autoheal:36`
It takes the shared lock, checks, then closes fd 9 and goes on. A deploy that starts a moment later can have its fresh containers restarted by autoheal, since `docker restart` can take up to the stop timeout. Hold the shared lock until the end of the run. A deploy then gets exit 3 at worst.

### 🟡 `receive` is unlocked and replaces a release in place, `deploy/host/kfs-deploy:49`
`rm -rf $RELEASES/$sha; mv` breaks a running release if you redeploy the current sha, or a deploy of that sha is mid-run. `current` dangles for that moment, and an interrupted receive leaves it dangling. Take the lock, and refuse or swap by rename when the sha is the current one.

### 🟡 A backup in progress is only detected when the answer is yes, `deploy/host/kfs-deploy:113`
If the `exec` fails for any reason the deploy goes ahead and recreates the backup container mid-run (`up -d` changes the image tag on every deploy). That night's backup is lost, and health stays green for 26 h after the container start. Treat an unexpected failure of the check as a refusal, or let the backup finish a run on SIGTERM.

### 🟡 Backup health is hidden by frequent deploys, `docker/backup/run.sh:334`
`health` is true for `BACKUP_STALE_H` after any container start. Every deploy recreates the container, so a daily deploy keeps the container green whatever the backups do. The healthchecks.io ping silence covers it. Say so in the spec, or make the grace period apply only before the first success.

### 🟡 `sudoers kfs-*` is root-equivalent, `deploy/host/bootstrap.sh:129`
With free arguments, `kfs-compose run -v /:/h ...` and `kfs-deploy receive` (an arbitrary bundle that then runs as root) both give root. Keeping `kfs` out of the docker group is therefore cosmetic. The real boundary is the Tailscale identity and the 12 h check. Document that instead of presenting it as least privilege.

### 🟡 Data disk move leaves most services on the parked folder, `deploy/host/bootstrap.sh:197`
Only the recorder is stopped and restarted. The compactor, trader, dashboard and backup keep their bind mount of the old directory, now renamed to the parked name, until they are recreated. Compactor output and the backup read the stale copy until the next deploy. A script death between stop and start also leaves the recorder down. Recreate every catalog user, or tell the operator to deploy right after.

### 🟡 Safe reboot leaves a stale marker if the reboot call fails, `deploy/host/kfs-safe-reboot:76`
`systemctl reboot` failing leaves the trader paused and the marker in place. The next, unplanned boot can lift a pause that is no longer a deploy pause (post-boot checks the source, which limits it). Delete the marker and unpause on failure.

### 🟡 No automated test of `kfs-deploy run` or `kfs-safe-reboot`, `scripts/tests/host-scripts-test.sh`
The drain, autoheal and kfs-compose are covered. Rollback, prune, the lock exit code, the signal behaviour above, `receive` and the post-boot decisions were proven only by hand on the VPS (verify.md). The suite also runs only by hand, not in CI. Add stub-docker cases for rollback and prune.

### 🟡 Docker apt repository key trusted on first fetch, `deploy/host/bootstrap.sh:236`
Docker and Tailscale keys are fetched with `curl` and no fingerprint check. The alpine base image of the backup is also not pinned by digest (`docker/backup/Dockerfile:6`). Low risk but a supply chain gap on a trading host.

## Nits
- ⚪ `deploy/host/kfs-autoheal:46`, `docker ps` can list one off `compose run` containers (control CLI, soak samples) that inherit a health check; `docker restart` on a `--rm` one is noise. Filter on `com.docker.compose.oneoff=False`.
- ⚪ `deploy/host/lib.sh:60`, `kfs_secret_names` greps compose files with a regex tied to the `${SECRETS_DIR...}/name` spelling; a differently spelled secret mount silently drops out of the preflight.
- ⚪ `scripts/deploy.sh:78`, `${disk[*]}` is interpolated into a remote command string; it is validated above, so fine, but a comment would help.

## Strengths
- The drain sequence is fail closed everywhere: an unconfirmed pause aborts and is lifted, a stale `trader_state` aborts, a lifted pause aborts, and `--now` is explicit. The unit-style shell tests cover each branch.
- Pause state lives in a Redis key written after the in-memory change, with unreadable keys resolving to paused (`node/control.py`), so a failure at either end stays safe.
- Secrets never reach a process argument: curl `-K -`, pgpass on tmpfs, `docker run -e NAME` with no value, tailscale `file:` key, and fingerprints only. The secrets folder is never mounted into the backup.
- The restore drill guards the prune against forged or hidden snapshots (age window, removal count, future dates), and the VPS key cannot delete.

## Test coverage
Python control side: consumer, pause, node control and the CLI are well covered, including Redis outages, duplicates, expiry, and the executor bug path. Missing: a crash between `received` and `execute` (finding 1) and an operator pause replaced during a deploy (finding 3). Host scripts: see the Minor finding above.
