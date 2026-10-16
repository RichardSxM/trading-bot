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
9. **Daily note (AC-18).** At 08:00 Amsterdam the next day a silent note arrives with counts by tier, down minutes, yesterday's trades and realized P&L, and the backup health. The numbers match the `alerts` and `positions` tables.
