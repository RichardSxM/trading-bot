# 0013. Verify: phone alerts & health checks

These are steps you may run to check the feature by hand. Part A runs on the laptop in demo with `ALERTS_DELIVERY=log` (dry run), after `docker compose build` and `docker compose up -d`. Bring the laptop stack down when you are done (the VPS runs the app). Part B runs on the VPS with real pushes to your iPhone. Each step names the acceptance criteria it checks.

Before Part B you need the accounts from the spec's Follow-up: Pushover (with Critical Alerts allowed for Pushover in iOS Settings, Notifications) and healthchecks.io with its three checks. Add the new `ALERTS_*` lines to `/etc/kfs/kfs.env` by hand before the deploy, or the deploy refuses and rolls back.

To read alert rows: `docker compose exec -T postgres psql -U postgres -d kfs -c "SELECT id, rule, severity, delivery, occurrences, sends, opened_at, resolved_at FROM alerts ORDER BY id DESC LIMIT 10"`.

## Part A: laptop, dry run

1. **Dry run path (AC-13).** `docker compose logs alerter` shows startup with `delivery=log` and no ping lines. `alerter test` (AC-17 form) prints three `alert_dry_run` lines, and three `test` rows appear with `delivery=dry_run`.
2. **Trader down and back (AC-1, AC-9).** Run `docker compose stop trader`. Within 60 s a `trader_down` row opens, critical, `dry_run`. Wait 2 min: still one row, no second dry run line. Start the trader: the row gets `resolved_at`, and a resolved note line is logged.
3. **Restart keeps dedupe (AC-9).** Stop the trader again, wait for the row, then `docker compose restart alerter`. No second `trader_down` row and no second dry run line. Start the trader.
4. **A critical risk event (AC-3).** Set `SANDBOX_REJECT_KILL_SWITCH_ORDERS=true`, recreate the trader, wait for a position, tap stop. After 20 s a `risk_critical` row with fingerprint `flatten_timeout:<id>` appears. Set the drill back and recreate.
5. **Recorder codes (AC-4, AC-7).** Run `docker compose stop recorder` for 3 min, then start it. A `service_down:recorder` row (critical) opens after 2 min and resolves after the start. When the compactor next runs, a `book_gap` line for the `recorder_down` gap gives one `book_gap_long:recorder_down` row, not one per instrument. `context_feed_down` (logged by the trader or the recorder when Kraken's feed drops, not when a process stops) is proven by the pair rule's unit tests here, and on the VPS during a Kraken maintenance window.
6. **Storm cap and coverage (AC-10, AC-19).** Run the core tests: `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q -k alerts`. The storm, dedupe, deploy window, and coverage tests pass. To see the coverage test bite, comment out one promised rule; it fails naming the code.
7. **Postgres down (AC-15).** `docker compose stop postgres`, then `docker compose stop trader`. The `trader_down` and `postgres_unreachable` dry run lines appear anyway. Start Postgres: both rows are written. Start the trader.
8. **Mute and test from the dashboard (AC-12, AC-16).** Log in, press Mute warnings 1 h. The badge shows the end time in Amsterdam time. Stop the recorder for 3 min: `container_unhealthy` or `service_down` rows appear; the warning ones show `delivery=muted`, the critical one `dry_run`. Press Unmute: one summary line for the warnings still open. Press Send test alert: three test rows. The Activity feed lists the episodes with tier and state.

## Part B: VPS, real pushes

1. **The push path (AC-17, AC-20).** `kfs-compose run --rm alerter alerter test`. Three pushes arrive: the critical one sounds with your phone on Do Not Disturb and repeats every minute until you acknowledge it in Pushover; the warning one notifies normally; the info one arrives silently. `kfs-compose logs alerter | grep -c pushover_app_token` prints 0.
2. **Bot down at night (AC-1).** With Do Not Disturb on, `kfs-compose stop trader`. Within 60 s a critical `[demo] Trader down` push sounds. Start it: a silent "resolved after N min" follows.
3. **VPS silent (AC-2).** `kfs-compose stop alerter`. Within 3 min healthchecks.io sends `alerter-alive` down through Pushover and email. Start it: healthchecks.io sends the recovery.
4. **Push failure path (AC-14).** Replace `pushover_app_token` with a wrong value and recreate the alerter. `alerter test` fails, the row is `failed`, and within 2 min healthchecks.io reports `alerter-alive` failing. Restore the token and recreate; the failed rows are sent on the next cycle.
5. **Deploy window (AC-11).** Deploy any release. No warning pushes arrive during it, a critical one would, and no `trader_down` arrives unless the trader is still down 15 min after the start. `deploys.jsonl` and the `held` rows match.
6. **Host and backup (AC-4, AC-5).** `grep autoheal /var/log/kfs/host.jsonl` shows autoheal lines. After the next nightly backup, healthchecks.io shows a `backup-verified` ping. Run the restore drill from the laptop with `./secrets/hc_restore_drill_url` in place: after PASS, `restore-drill` shows a ping. `df -h /` matches the free space the alerter logs at start.
7. **Stuck entry block (AC-6).** Stop the recorder's Postgres writes long enough for `recording_unhealthy`, or set `ALERTS_ENTRY_BLOCK_S=120` for this step and cause a `stale_feed` block. After the threshold an `entry_block:<reason>` warning arrives in plain words; it resolves when the block clears. Set the threshold back.
8. **Live fills (AC-8).** Only once live trading starts (feature 19): each fill arrives as a silent push with side, size, price, and fee. In demo, check that no fill pushes arrive.
9. **Daily note (AC-18).** At 08:00 Amsterdam the next day a silent note arrives with counts by tier, down minutes, yesterday's trades and realized P&L, and the backup health. The numbers match the `alerts` and `positions` tables. Restart the alerter after the note: no second note arrives, and the `alerts` table holds one `daily` row for today.

## Part C: Milestone 3 on the laptop (host lines, disk, backup, block age, context hours) · added 2026-10-16

These steps fake the VPS's `/var/log/kfs` so you can check the host side without the VPS. You may run them after `docker compose build alerter backup trader recorder` (one service at a time on the Mac) and `scripts/dev-secrets.sh` (it adds the `hc_backup_url` placeholder). Read rows as the database owner: `docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`. Bring the stack down afterwards.

### UI / manual

- [x] **Fake host folder.** Make a folder with empty `host.jsonl` and `deploys.jsonl` (mode 0644), and a Compose override that binds it read only at `/host/kfs-log` in the alerter. Start `trader alerter` with the override and `ALERTS_ENTRY_BLOCK_S=60`. → the `alerter_started` line shows `host_lines: true`. → AC-4, AC-11
- [x] **Deploy window holds, then summarises.** Append a `host.jsonl` line `{"msg":"deploy started","sha":"<sha>","started_at":"<now>",...}`. Stop the trader, and every 5 s write a fresh `demo:snapshot:trader_state` naming `stale_feed`, with `entries_blocked_since` 2 h back (redis-cli as the `trader` user). → no `alert_dry_run` line for `trader_down` or `entry_block` during the window. Append the `deploys.jsonl` line with the same `sha` and `started_at`. → one warning "1 warning during deploy" naming "Entries blocked for over an hour", then a critical `trader_down`. The `entry_block` row shows `sends=1` and `details.since` 2 h back. → AC-11, AC-6
- [x] **Silent resolves.** Start the trader. → info "Resolved:" lines for `entry_block:stale_feed` and `trader_down`. → AC-6, AC-1
- [x] **Host criticals, once each.** Append `{"msg":"autoheal_gave_up","container":"x",...}` and `{"msg":"rollback failed","sha":"cafe"}` to `host.jsonl`, and `{"sha":"cafe",...,"outcome":"rollback_failed"}` to `deploys.jsonl`. → exactly one critical `autoheal_gave_up:x` and one critical `rollback_failed:cafe`; the host's "rollback failed" line pushes nothing. → AC-4
- [x] **No replay after a recreate.** Recreate the alerter. → none of the old host lines push again. → AC-11
- [x] **Disk low.** Recreate the alerter with `ALERTS_DISK_FREE_GB=100000`. → one critical `disk_low` saying "N GB free on the host root filesystem (the floor is 100000 GB)", where N matches `df` inside Docker's disk. → AC-4

### Commands

- [x] `TRADING_ENV=demo docker compose run --rm -T --no-deps backup kfs-backup once` → `backup done`, `backup verified` with a small `newest_age_s`, and `backup-verified ping skipped` (the laptop placeholder). A failing step's line carries `"code":"backup_failed"` with its `step`. → AC-5
- [x] `docker compose run --rm -T migrate pytest -q -m integration tests/integration -k backup_app` → passes, including every sequence being readable by `backup_app` (`pg_dump` needs `alerts_id_seq`). → AC-5
- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_alerts_hold.py tests/test_alerts_checks.py tests/test_marketdata_context_hours.py` → pass: hold and summary rules, a critical passing a deploy, `trader_down` held then pushed, the 15 min end; disk; the entry block set, its since, and the 5 min restart rule (a reason back after 7 min starts over). → AC-4, AC-6, AC-11
- [x] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_compactor_context.py -k "closed_hour or first_check or feed_outage"` → one `context_hour_missing` per missing table for an hour followed by a finished book hour, nothing on the first check or on a rerun, and an outage hour excusing `open_interest_update` only. → AC-7
- [x] `docker compose run --rm --no-deps -T alerter pytest -q tests/test_host_sources.py` → the file follower starts at the end, waits for a newline, and rereads after truncation or a new inode; a stale or missing `trader_state` changes nothing; the ping waits for a fresh host read. → AC-2, AC-4, AC-6

### Value sourcing checks

- [x] `entry_block` since: vary `entries_blocked_since` (2 h back, now, in the future, missing). → the push comes at once, after `ALERTS_ENTRY_BLOCK_S`, or counts from now in the last two cases. → AC-6
- [x] `disk_low` free GB: a value just under the floor (for example 4.99 GB under 5) reads 4.9, never 5.0. → AC-4
- [x] Deploy window start: on the VPS, the "deploy started" line in `host.jsonl` carries the same `started_at` as that deploy's `deploys.jsonl` line, so the window closes on the outcome line, not after 15 min. → AC-11 (2026-10-16, deploy of b8949f8: both lines carry `started_at` 2026-10-16T23:09:54Z)

### Acceptance criteria coverage (Part C)

- AC-2 by the host ping gating test · AC-4 by host criticals, disk low, and Commands 3 and 5 · AC-5 by the backup run and the sequence grant test · AC-6 by the deploy window step, silent resolves, and the since checks · AC-7 by the compactor test · AC-11 by the deploy window, no replay, and the start time check

## Part D: Milestone 4 tasks 4.1 to 4.3 (fills, alert commands, dashboard) · added 2026-10-16

_The laptop half runs in dry run with `alerter dashboard` up (no trader or recorder needed). To send a command without the dashboard login, XADD it as the `dashboard` Redis user from inside the dashboard container (a short script using `trading_core.control.alerts.alert_command` and `command_to_fields`)._

### UI / manual

- [x] **Mute holds warnings, never criticals.** On Overview, tap Mute warnings · 1 h. → the alert area says "Mute for 1 h sent", then the Activity head shows "Warnings muted until HH:MM" in Amsterdam time (UTC in the tooltip). A warning while muted gets `delivery=muted` and no `alert_dry_run` line; a critical still logs `alert_dry_run`. → AC-12, AC-16
- [x] **Unmute sends one summary.** With a warning still open under the mute, tap Unmute. → the badge goes away and one warning "N warnings while muted" listing its title is pushed (or logged). → AC-12
- [x] **A mute that runs out.** Mute 1 h, then wait it out (or set the key's TTL short with redis-cli as the `alerter` user and restart the alerter). → the summary comes on its own and the badge hides once `until` is past. → AC-12, AC-16
- [x] **A mute survives an alerter restart.** Mute 4 h, then `docker compose restart alerter`. → the log has `alert_mute_restored` with the same `until`; warnings stay muted. → AC-12
- [x] **Test button.** Tap Send test alert. → within a few seconds "Test sent: one push per tier", three `test:<id>:<tier>` rows (critical, warning, info), even while muted. With the alerter stopped, the button says "no answer from the alerter" after 10 s. → AC-16
- [x] **Activity feed.** → the last 24 h of episodes sit between the commands by time, each with its tier badge, title (no `[demo]` tag), open or resolved, and the delivery word (`dry run` on the laptop). Stop Postgres: the commands stay and a row says "unavailable: Postgres did not answer". → AC-16
- [ ] **VPS: a live fill.** In `live`, after a fill. → one silent push "Bought|Sold <qty> <base> at <price>, fee <fee> <currency>", quantity and price at the instrument's lot and tick precision. → AC-8

### Commands

- [x] `docker compose run --rm migrate` then, as `trading`, `\dp instruments` → `alerter_app=r`. → AC-8
- [x] `docker compose run --rm -T migrate pytest -q -m integration tests/integration -k "fill_fields or privileges"` → the fill lookup as `alerter_app` reads the fill and its instrument, and the grant table matches. → AC-8
- [x] `docker compose run --rm --no-deps -T alerter pytest -q tests/test_fills.py tests/test_commands.py` → a live fill already stored pushes at once, one stored late is found on a later read, one never stored pushes its trade id after six reads over 10 s, a demo fill is never read or pushed; mute, unmute, replace, expiry summary, expired, `failed invalid`, test per tier, `push_failed`, restart restore. → AC-8, AC-12, AC-16
- [x] `docker compose run --rm -T dashboard pytest -q -m redis` and `docker compose run --rm -T alerter pytest -q -m redis` (after `docker compose up -d redis` picks up the ACL) → the dashboard appends to `*:control:alerts` and reads `*:control:alerts:results`, and is refused SET, DEL, and GET on `*:alerts:mute` and XADD on the results; the alerter reads the command stream in group `alerter`. → AC-12, AC-20
- [x] `docker compose run --rm --no-deps -T dashboard pytest -q tests/test_alerts_ui.py` → mute lengths 1, 4, 12, unmute 0, other lengths 422, CSRF refused 403, Redis down 503, the badge from the newest executed mute or unmute, the test answer, timeout, and failure, and the feed merge. → AC-12, AC-16

### Value sourcing checks

- [x] Fill push values: vary `lot_size` (0.0001, 1) and `tick_size` (0.5, 1, 0.01). → quantity and price show exactly that many decimals; a fee of 0.0214 shows all four, a fee of 0.1 shows 0.10. → AC-8
- [x] Mute badge until: mute at 22:30 UTC in October (summer time) and in January. → the badge shows UTC+2 and UTC+1 Amsterdam times; the tooltip shows the UTC time. → AC-16
- [x] Mute end in the engine: the mute ends at the command's `issued_at` plus its length, also when the alerter reads it late. → AC-12

### Acceptance criteria coverage (Part D)

- AC-8 by the live fill step, the grant, the fill tests, and the precision check · AC-12 by the mute, unmute, expiry, restart steps and the command tests · AC-16 by the badge, test button, feed steps, and the dashboard tests · AC-20 by the ACL suites

## Part E: Milestone 4 task 4.4 (the daily note) · added 2026-10-16

These add to step 9 in Part B. You may run the laptop steps after `docker compose build alerter`, with `ALERTS_DAILY_AT` set a minute or two ahead of the current Amsterdam time. Bring the stack down afterwards.

### UI / manual

- [x] Set `ALERTS_DAILY_AT` two minutes ahead and recreate the alerter. → at that minute one `daily_note` log line, one `daily` row with fingerprint `daily:<today>`, `resolved_at = opened_at`, `delivery=dry_run`. → AC-18
- [x] Restart the alerter the same day. → no second `daily` row and no second log line. → AC-18
- [x] Set `ALERTS_DAILY_AT` a few hours in the past and start the alerter on a day with no `daily` row. → the note goes out once within a minute (the catch up). → AC-18
- [x] Send a 1 h mute, then trigger the note. → the note still goes out; its row is never `muted` or `held`. → AC-18, AC-12
- [x] Stop Postgres, let the note come due, wait two minutes, start Postgres. → `daily_note_waiting` warnings while it is down, then one note once it answers. → AC-18

### Commands

- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_alerts_daily.py` → green → AC-18
- [x] `docker compose run --rm --no-deps -T alerter pytest -q tests/test_daily.py` → green → AC-18
- [x] `docker compose run --rm -T migrate pytest -q -m integration tests/integration/test_alerts_repository.py` → green, the figures read as `alerter_app` → AC-18

### Value sourcing checks

- [x] When to send: the note goes out at 06:00 UTC in summer time (October before the last Sunday) and at 07:00 UTC in winter time. → 08:00 Amsterdam both times. → AC-18
- [x] Trades and realized P&L yesterday: a position closed at 23:59 UTC counts toward that UTC day; one closed at 00:30 UTC the next day does not. → the count and P&L match `positions` for yesterday's UTC day only. → AC-18
- [x] Trader down minutes: a `trader_down` episode that opened more than 24 h ago and is still open (or resolved inside the window) counts only the part inside the last 24 h. → AC-18
- [x] Counts by tier: `daily` rows are left out; `test` rows still count. → the opened counts match `alerts` rows of the last 24 h minus the `daily` ones. → AC-18
- [x] Sent today: the marker is the `daily:<local date>` row in this `env`, whatever its delivery; a `failed` note row still blocks a second note and is retried by the 60 s cycle. → AC-18, AC-14

### Acceptance criteria coverage (Part E)

- AC-18 by every step above and Part B step 9 · AC-12 and AC-14 by the mute and failed marker checks
