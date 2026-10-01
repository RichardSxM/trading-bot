# 0003. Verify: API keys & secrets

Steps you may run to prove the build against [index.md](index.md), then the runbook for the real secrets. Each step names the criteria it checks. Laptop steps use `TRADING_ENV=demo` unless they say otherwise.

## Build checks (laptop)

- [x] **1. Generate dev secrets.** `scripts/dev-secrets.sh`: the nine files named in index.md appear in `./secrets/` with mode `-r--r--r--`, and the folder has `drwx------`; run it again and nothing changes; `ls secrets | grep -c kraken` prints `0`. (AC-14)
- [x] **2. Cut over the existing database.** `docker compose up -d postgres`, then `scripts/set-pg-owner-password.sh`, then `docker compose run --rm migrate`: role setup and Alembic succeed. (AC-11)
- [x] **3. Stack boots on files.** `docker compose up -d`: every service healthy; `docker compose logs | grep 'secret loaded'` shows one line per secret for migrate (4), trader (2), dashboard (2), and backup (4), each with an `fp`; postgres logs none. (AC-1, AC-3)
- [x] **4. Nothing in the environment.** `docker compose config | grep -iE 'password|_key|secret' ` shows only file paths and `*_FILE` names, no values; `for s in postgres migrate trader dashboard backup; do docker inspect $(docker compose ps -aq $s) --format '{{json .Config.Env}}'; done` shows no password or key. (AC-1)
- [ ] **5. Only the listed services get each file, and each can read it.** `docker compose exec dashboard ls /run/secrets` lists `postgres_password redis_password`; `recorder` has no `/run/secrets`; `trader` has no Kraken file in demo. For every Python service and backup, `docker compose exec <service> sh -c 'for f in /run/secrets/*; do test -r "$f" || echo "unreadable $f"; done; id -u'` prints only `10001` (or the backup's uid). Run this on the laptop and again on the VPS. (AC-1, AC-14)
- [x] **6. Missing secret fails closed.** Move `secrets/postgres_dashboard_password` away and `docker compose up dashboard`: exit 1, one line with `code: secret_unavailable`, `reason: missing`. Repeat with an empty file (`empty`), a directory (`not_a_file`), `abc` (`too_short`), and `aaaaaaaa bbbbbbbb` (`whitespace`). Then empty `secrets/redis_dashboard_password` and recreate `redis`: it exits 1 with `secret_unavailable` instead of starting with an empty password. Restore it. (AC-2)
- [x] **7. Redaction.** The unit tests in `packages/core/tests/test_secrets.py` log a loaded value in a message, a field, an exception, and URL quoted: every line shows `[REDACTED:<name>]`. (AC-5)
- [x] **8. Env guard everywhere.** `docker compose run --rm --no-deps -e TRADING_ENV=live -e KRAKEN_FUTURES_API_KEY=x trader python -m trader.main`: `env_guard_refused`, exit 1. (AC-4)
- [x] **9. Canary leak test.** With `IMAGE_TARGET=dev`, `docker compose run --rm --no-deps -T trader pytest -q -m canary`: passes (exit 1 and `kraken_auth_failed` seen, no canary value in the output). Breaking the redaction on purpose makes it fail. The key check unit tests (fake cache, `TestClock`) pass for both the confirmed and the timeout path. (AC-6, AC-7, AC-8)
- [x] **10. Key age.** The trader settings tests: `KRAKEN_KEY_ROTATED_ON` missing, `2026-13-01`, or tomorrow in live → refused; 91 days ago → one `kraken_key_rotation_due` warning with `age_days: 91`; in demo it is not required. (AC-9)
- [x] **11. Redis permissions.** `docker compose exec redis redis-cli GET x` → `NOAUTH` or `NOPERM`; `redis-cli PING` → `PONG`; `redis-cli --user dashboard --pass "$(cat secrets/redis_dashboard_password)" SET x 1` → `NOPERM`; same user `XADD demo:control '*' k v` → an id; `--user trader … FLUSHALL` → `NOPERM`. (AC-10)
- [x] **12. Trader reloads under its user.** The Redis integration test writes a cache entry as the trader, restarts the node, and reads it back; then `docker compose restart trader` by hand: healthy again, no `NOPERM` or auth error in its log. (AC-10)
- [x] **13. Backup gets its keys.** `docker compose logs backup`: four `secret loaded` lines, then `backup not configured`. (AC-12)
- [x] **14. gitleaks.** Put a fake key in a scratch file, `git add` it, `git commit`: the hook blocks it. Push a branch with one to see CI fail, then delete the branch. (AC-13) _Done 2026-10-01: the hook blocked a random fake key (`kraken-api-secret`); pushed with `--no-verify` on `scratch/gitleaks-canary`, CI run 36931484359 failed at gitleaks (`leaks found: 1`, checksum `OK`), branch deleted. CI run 36931387463 on `feat/api-keys-secrets` 5ee2fc3 passed every step, canary included._
- [x] **15. History scan recorded.** [rationale.md](rationale.md#history-scan) has the scan result. (AC-13)

## Runbook (AC-15)

### Place secrets on the VPS

1. `sudo install -d -m 0700 -o root -g root /etc/kfs/secrets`.
2. For each inventory file, copy the value from your password manager into it without a trailing newline (`printf %s '<value>' | sudo tee /etc/kfs/secrets/<name> >/dev/null`, from a shell with history off), then `sudo chown 10001:10001` and `sudo chmod 0400`.
3. Set `SECRETS_DIR=/etc/kfs/secrets` in the VPS `.env`.
4. `docker compose up -d` and check step 3 above.

### Kraken key creation checklist

Run at feature 19, on Kraken's futures site, from your own device:

- [ ] General API: **Full access** (needed to place and cancel orders).
- [ ] Withdrawal API: **No access**.
- [ ] IP restriction set to the VPS's public IPv4, if the page offers it.
- [ ] Screenshot the permission page and attach it to the password manager entry with the key, the secret, and the creation date.
- [ ] Place `kraken_api_key` and `kraken_api_secret` on the VPS (see above) and set `KRAKEN_KEY_ROTATED_ON=<creation date>` in the VPS `.env`.
- [ ] Start with `docker compose -f compose.yaml -f compose.live.yaml up -d trader`; expect `kraken account confirmed` within `KRAKEN_KEY_CHECK_TIMEOUT_S` (60 seconds by default).

### Rotation

- **Kraken key, every 90 days or on suspicion.** Prefer a moment with no open position (until feature 11 makes restarts safe). Create the new key with the checklist, replace both files, update `KRAKEN_KEY_ROTATED_ON`, restart `trader`, check the new `fp` and `kraken account confirmed`, then delete the old key on Kraken.
- **Postgres role password.** New value into the password manager and the file, `docker compose run --rm migrate`, restart the service that uses it.
- **Postgres owner password.** New value into the file, `scripts/set-pg-owner-password.sh`, then `docker compose run --rm migrate`.
- **Redis user password.** New value into the file, `docker compose up -d --force-recreate redis`, then restart `trader` and `dashboard`.
- **Restic and B2.** Owned by feature 7's backup routine; replacing the B2 key means a new bucket scoped key in Backblaze, then replace the file and restart `backup`.

### Leak drill

1. **Revoke the Kraken key on Kraken's site first**, from your own device. Exchange side stops keep protecting open positions while the bot is cut off.
2. Check the Kraken account for orders or trades you did not make, and close anything unwanted by hand.
3. Rotate every secret that may have been exposed (see Rotation), starting with any that sat in the same place as the leak.
4. Find the leak path (log, repo, screenshot, VPS), remove it, and if it was a log or the repo, add a test or a gitleaks rule that would have caught it.
5. Record what happened in `docs/` (a `/document postmortem`).

## Build notes (added by /develop, 2026-10-01)

What the build found that changes how some steps above read:

- **Step 3 counts.** Migrate logs five `secret loaded` lines per run: four from role setup, then the owner password again from Alembic, which is its own process.
- **Step 6, a missing file.** Docker itself refuses to start a service whose secret file is missing (`bind source path does not exist`), before our code runs; that still fails closed. The `missing` reason is what our loader reports when the file vanishes inside a running container, and what the unit tests cover.
- **Step 9 timing.** The canary run takes about 70 seconds: NautilusTrader waits its own 60 second connection timeout before it acts on the stop.
- **Key check mechanism.** When the Kraken client cannot connect, NautilusTrader never starts the trader, so an actor never runs. The key check is therefore a task on the node's event loop, not an actor; index.md describes it (updated 2026-10-01).
- **Step 12, done by hand.** After the Redis ACL went live, a trader restart logged in as `trader` and cached 281 currencies and 302 instruments from Redis, exactly the 583 keys counted before the restart, with no auth error. There is no automated restart test; `apps/trader/tests/test_redis_acl.py` covers the denials.
- **Laptop cutover, done once.** The existing `pgdata` volume kept its old owner password; `scripts/set-pg-owner-password.sh` moved it to the file value.
- **History scan.** One false positive (a fake 32 character test value in commit `9d469e1`) is listed in `.gitleaksignore`; see [rationale.md](rationale.md#history-scan).

