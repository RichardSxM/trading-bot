# Review, feat phone alerts (spec 0013), 2026-10-17

**Reviewed by**: Sonnet 5.5 (author model not stated)
**Scope**: 127 files (uv.lock skipped), range 1dd6649..8ff0a1f (PRs #56 and #57). The working tree code under packages and apps is identical to 8ff0a1f.
**Verdict**: Changes requested

## Summary
This adds the alerter service: a pure rule table and episode engine in core, Pushover delivery, healthchecks.io pings, a Postgres `alerts` table, Redis commands for mute and test, the dashboard controls, and host and backup changes. The core engine is careful and well tested. The problems sit at the edges: log lines can be lost for good when a read pass partly fails, a restart can push a critical alert twice because delivery does not wait for the stored episodes to load, and the Docker proxy setting gives the alerter more reach than the spec says. The known `entry_block` title issue is confirmed and ranked Minor.

## Major
### 🟠 Log lines are lost for good when a read pass partly fails, `apps/alerter/src/alerter/sources/docker_logs.py:136`
**Problem**: `_read` marks every new line as seen (`self._seen`, line 177) before it writes the cursor to Redis (line 181). Two things then throw the lines away while the "seen" marks stay. First, if `cursors.set` raises `RedisError`, the `found` list is never returned, and on the next pass the same lines are skipped because they are already in `_seen`. Second, in `poll` (line 159) a failure in one container makes the whole call raise after the other containers were read, so their observations are dropped but their cursors and `_seen` entries have already moved on. `Alerter.read_logs` catches the error and returns nothing.
**Why it matters**: A CRITICAL `stop_not_accepted`, `exit_failed` or `abandoned` line that arrives in the same pass as a Redis hiccup, or a 404 for a container removed mid pass (every deploy recreates containers), never becomes a push. That breaks AC-3 for exactly the lines that mean money is at risk, and nothing logs the loss.
**Suggested fix**: Record `_seen` and move `_positions` only after the lines are handed back. On a partial failure, return what was read (and still raise or flag the failure for the watchdog), or keep the failed container's work out of the shared state. Add tests for both failure paths: a Redis failure on the cursor write and a Docker 404 for one container while another has a critical line.
**Checked 2026-10-17**: read against the code; both paths are as described.

### 🟠 A restart can push an open critical alert again, `apps/alerter/src/alerter/service.py:370`
**Problem**: `run` starts every loop together. `StoreWriter.run` loads the stored open episodes into the engine, but the heartbeat, disk, container and log loops do not wait for `store.loaded`. The Redis heartbeat read usually returns before the Postgres load finishes. A `trader_down` or `disk_low` condition that is still true opens a new in memory episode with `sends=0` and pushes. When the load arrives, `engine.load` keeps the new episode and closes the stored one as stale (`store.py:104`).
**Checked 2026-10-17**: read against the code. `run` only waits for `read_host`; the store task loads in a thread while the other loops start. The stale close logic in `load_once` shows that running before the load is on purpose (so alerts still work with Postgres down), which is why a bounded wait fits better than a hard one.
**Why it matters**: AC-9 says "after an alerter restart, open episodes load from Postgres, and nothing is pushed twice". With Postgres slow or down at start, a critical (priority 2, repeating) push goes out a second time for an episode you already acknowledged. The existing restart test loads the engine by hand first, so it does not see this.
**Suggested fix**: Give the first load a short bounded wait before the check loops start (for example up to 10 or 15 s), then carry on from memory if Postgres is still down, which keeps AC-15. Add a test that starts the loops with a slow load and a stale heartbeat.

### 🟠 The Docker socket proxy gives more than the spec says, `compose.yaml:447`
**Problem**: The spec (Security model) says `CONTAINERS=1` exposes "container listings, inspect, and logs" and that secrets are safe because they are files, not env vars. With `tecnativa/docker-socket-proxy`, `CONTAINERS=1` allows every GET under `/containers`, including `/containers/<id>/archive` and `/containers/<id>/export`. Secrets are bind mounted files under `/run/secrets`, and the archive call reads files from mounts. **Confirmed 2026-10-17**: the same image (`v0.5.0`) with the same settings, run on a throwaway network, answered `GET /containers/<id>/archive?path=/marker.txt` with HTTP 200 and the file's contents, and `GET /containers/<id>/export` with HTTP 200 and the whole filesystem as a tar. `GET /images/json` got 403, so only the `/containers` rule is too wide.
**Why it matters**: A compromised alerter (it parses untrusted log text and talks to the internet) could read the Kraken keys from the trader container on the VPS. The spec's security claim would then be wrong.
**Suggested fix**: Confirm with a test call. If it works, use a custom haproxy config (or a small allow list proxy) that permits only `GET /containers/json`, `/containers/<id>/json` and `/containers/<id>/logs`. Update the spec text either way.

## Minor
### 🟡 Entry block title ignores `ALERTS_ENTRY_BLOCK_S` (known finding, confirmed), `packages/core/src/trading_core/alerts/messages.py:176`
**Problem**: The title is always "Entries blocked for over an hour". The setting (`settings.py:228`) defaults to 3600 but allows 60 and up, is passed through `apps/alerter/src/alerter/settings.py:46` and drives the delay in `engine.py:394`. At 300 s the push still says "over an hour". `test_alerts_checks.py:99` locks the fixed text in.
**Why it matters**: It is a wrong statement in a push, but only when you change the default. The AC-6 text is satisfied at the default. That is why it is Minor and not Major.
**Suggested fix**: Build the title from the configured length (pass it in through the check fields) or use a neutral title such as "Entries blocked", then update the test.

### 🟡 Retry age counts from `opened_at`, so long open episodes lose their retries, `packages/core/src/trading_core/alerts/engine.py:332`
**Problem**: `retry` drops any failed episode whose `opened_at` is older than 24 h. A critical condition open for days whose 4 hour re-push fails is removed from `_failed` at once. It is never retried, and `push_failing` turns false, so the watchdog goes back to the plain ping.
**Why it matters**: The spec says a row that has been failing for 24 h stops retrying, not a row that was opened 24 h ago. A broken push path is hidden for exactly the episodes that matter most.
**Suggested fix**: Track when the push first failed and age from that.

### 🟡 Push tasks swallow unexpected exceptions, `apps/alerter/src/alerter/service.py:301`
**Problem**: `_spawn` keeps the task but never looks at its result. If `deliver` raises something other than the HTTP errors Pushover catches, nobody logs it, `_inflight` keeps the episode key, and `tick` skips that episode forever.
**Why it matters**: A critical push can quietly never go out and never retry.
**Suggested fix**: Wrap the task body in a try that logs `alerter_task_crashed` and clears the in flight mark (marking the row failed so the retry cycle picks it up).

### 🟡 One bad row can block all alert storage, `apps/alerter/src/alerter/store.py:164`
**Problem**: `flush_one` retries any `SQLAlchemyError` forever and always takes the head of the queue. A permanent data error (for example a NUL character in a log field that reaches `details` or `title`) repeats every 5 s, the queue fills to 1,000, and everything behind it is dropped. Separately, if a resolved old episode and a new episode with the same fingerprint are queued during a Postgres outage and `record` moves the old one behind the new one, the new insert hits `uq_alerts_open`, raises `AlertConflictError`, and is dropped after one log line.
**Why it matters**: Pushes still go out, but the Activity feed, the daily note counts, and restart dedupe stop being true.
**Suggested fix**: Treat only connection errors as "retry the same row". For a data error, log and drop that row (or move it to the back). Keep an episode's updates in order, or retry a conflict after the older row is saved.

### 🟡 The test button has no rate limit and each press is a repeating priority 2 push, `apps/dashboard/src/dashboard/web/alert_routes.py:112`
**Problem**: Each press sends a critical test that Pushover repeats every 60 s for an hour until acknowledged. Session and CSRF protect it, but a double tap or a stuck HTMX retry queues several.
**Why it matters**: Easy way to make your phone scream for hours. Also a small abuse path for a stolen session.
**Suggested fix**: Ignore a new test for about 30 s after the last one, in the alerter or the route.

### 🟡 A slow test command blocks the command reader, `apps/alerter/src/alerter/service.py:341`
**Problem**: `execute` sends the three test pushes one after another inside the consumer loop. With Pushover failing, each can take about two minutes, so a mute or unmute behind it waits several minutes. If the result append fails after the pushes, the pending entry is read again and sends three more tests.
**Why it matters**: A mute you pressed during a Pushover outage does not apply when you expect it.
**Suggested fix**: Run the test pushes as spawned tasks and answer from their outcome, or send them together.

### 🟡 `save` lets a stale write overwrite `delivery`, `packages/core/src/trading_core/recording/alerts.py:56`
**Problem**: The module says a stale or replayed save cannot undo a newer one. Counts and times use `greatest`, but `delivery` is always overwritten with the incoming value.
**Why it matters**: A late write of an older state can turn a stored `sent` back into `pending`, which shows wrongly in the feed and in the daily note.
**Suggested fix**: Only move `delivery` forward (for example when `sends` or `last_seen_at` is not older), or match the docstring to the behaviour.

### 🟡 An unrelated trader change rides along, `packages/core/src/trading_core/node/strategy.py:200`
**Problem**: The strategy now subscribes to the context feed topics on the message bus instead of `subscribe_data`. It is not in spec 0013, and it changes how the trader gets market context.
**Why it matters**: It may be right (it removes an error on every start), but it is a trader change reviewed under an alerts spec.
**Suggested fix**: Say so in the spec or PR text, or split it out.

## Nits
- ⚪ `scripts/restore-drill.sh:202`, the restore drill ping is sent before the retention dry run checks, though AC-5 says "after a PASS"; move it to the end or say that retention is not part of PASS.
- ⚪ `packages/core/src/trading_core/control/alerts.py:14` and `packages/core/src/trading_core/recording/alerts.py:22`, import private helpers (`_check_version`, `_strings`, `_utc`, `_uuid`, `_check_env`, `_table`) from sibling modules; make them public.
- ⚪ `packages/core/src/trading_core/alerts/messages.py:14`, the "pure" alerts package reads a global redaction registry from `secrets`; fine, but `test_core_boundary.py` does not list `alerts/` as a pure folder, so the purity claim is not enforced.
- ⚪ `apps/alerter/src/alerter/service.py:338`, a mute with no `mute_s` would fall through to the test branch; the parser rejects it first, but an explicit `else` that fails closed is safer.
- ⚪ `compose.yaml:447`, `docker-proxy` has no healthcheck, `cap_drop` or `no-new-privileges`, unlike the app services.
- ⚪ `packages/core/src/trading_core/alerts/engine.py:355`, the storm cap tracks pushes by `id(push)`; ids can be reused after garbage collection, and an unreported push stays counted for good. Key it on the episode key and reason.

## Strengths
- The engine is a clean pure state machine with explicit holds, cap, in flight tracking and replay safety; the rule table with the AC-19 coverage test and `promises.py` keeps future codes from being forgotten.
- Security basics are done well: secrets only as files, loaded in `pushover` mode only, redaction before cut, least privilege Postgres and Redis roles (INSERT revoked from `trader_app`), URLs passed to curl on stdin, httpx request logging turned down.
- Failure design is thought through: memory queue for Postgres down, `/fail` ping, daily note marker written before the push, mute restored from Redis after restart, host file follower that survives truncation.
- Scripts and migrations are additive, reversible and commented with the spec link.

## Test coverage
Core is well covered: engine open, repeat, re-notify, resolve, retry, cap, holds, summaries, daily, rules, messages, checks, control formats, repository integration, roles and migration round trip. Alerter app tests cover Pushover retries, the service loops, host files, commands and settings. Gaps that match the findings: no test for a partly failed log pass or a Redis failure while the cursor is written (the existing failure tests only check that the pass raises), no test for starting the loops before the store has loaded, no test for the retry age rule on a long open episode, no test for a permanent database error at the head of the queue, and no test that a spawned push task crashing is surfaced. The test at `test_alerts_checks.py:99` asserts the fixed "over an hour" title.
