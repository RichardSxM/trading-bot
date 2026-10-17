# 0013. Push alerts to the iPhone through Pushover from one alerter that reads the logs, with healthchecks.io watching from outside

**Date**: 2026-10-16
**Status**: In Progress

## Summary

A new `alerter` service reads every container's JSON logs, the host scripts' log lines, and the trader's Redis heartbeat. It turns the codes that earlier specs promised to alert on into pushes to your iPhone through Pushover. Critical alerts break through Do Not Disturb and repeat until you acknowledge them. Warnings notify normally. Info (a live fill, a resolved note, the daily summary) arrives silently.

Silence is caught from outside: the alerter pings healthchecks.io (a free "dead man's switch" service that alerts when pings stop) every minute, and the backup and the restore drill ping it too. So a dead VPS still reaches your phone.

Every alert episode is a row in a new Postgres `alerts` table, which keeps dedupe right across restarts and feeds the dashboard's Activity feed. The dashboard also gains a mute control and a test button. The laptop runs the same alerter in dry run mode, where pushes are logged, not sent.

## Requirements

**User stories**:
- As the bot's owner, I want my phone to wake me when money is at risk (a stop not accepted, an exit failing, the bot down), so a fault at night costs minutes, not hours.
- As the owner, I want warnings about the recorder, the feeds, and the host to reach me without waking me, so I can fix them the same day.
- As the owner, I want to hear when the whole VPS goes silent, even though nothing on it can tell me, so a dead server never goes unnoticed.
- As the owner, I want one push per problem, not a storm, and a quiet note when it clears, so I trust every push I get.
- As the owner, I want to mute known noise (a deploy, Kraken maintenance) without ever muting the critical alerts.

**Acceptance criteria** (the contract, each criterion is IDed and independently checkable):

*Reaching the phone*

- **AC-1** (trader down): when `{env}:snapshot:heartbeat` is missing, or its `ts_ns` is older than `ALERTS_TRADER_DOWN_S` (45 s), a critical `trader_down` push reaches the phone within 60 s of the trader's last heartbeat write. When a fresh heartbeat returns, a silent "resolved after N min" note follows. While Redis itself is unreachable, `trader_down` is not raised (it can't be known); `redis_unreachable` (AC-4) is raised instead.
- **AC-2** (silence from outside): the alerter pings the healthchecks.io check `alerter-alive` every 60 s, but only while the Redis check and every Docker log reader have succeeded within the last 60 s. Postgres being down does not stop the ping (alerts still deliver, AC-15). While any push is failing, it pings `alerter-alive/fail` instead (AC-14). If the VPS, Docker, the alerter, or its Redis link stops, healthchecks.io sends its own alert through its Pushover integration and email within its period plus grace (1 min + 2 min). This is the one alert allowed to take up to 3 min.
- **AC-3** (money at risk): every trader log line at level CRITICAL that carries an `event_key` produces a critical push within 60 s of the line, with `event_key` as the fingerprint. Today that covers `stop_not_accepted:*`, `exit_failed:*`, `abandoned:*`, a critical `halt:*` (including a loss limit halt), `halt_key_write_failed:*`, `flatten_timeout:*`, and `liq_near:*`. The coded CRITICAL lines `loss_limit_halt` (fingerprint `halt:<halt_id>`, so it merges with its `halt` event into one push), `positions_abandoned`, `kill_switch_task_crashed`, and `halt_key_invalid` are critical too. So is the trader's ERROR line `record_conflict`, which carries no `event_key` (the key lives only on its `risk_events` row); its fingerprint is the code, so conflicts on several tables make one push. A WARNING `halt` line (your own stop) is an info push.
- **AC-4** (host, deploy, storage): critical for `autoheal_gave_up`, a deploy outcome `rollback_failed`, free space under `ALERTS_DISK_FREE_GB` (5 GB) on the host root filesystem, `catalog_disk_full`, `recorder_write_failed`, Postgres or Redis unreachable from the alerter for 60 s, and a core service (`trader`, `recorder`, `postgres`, `redis`, `clock`) not running for 2 min. Warning for `backup_failed` (any step), the backup container unhealthy, and any other container unhealthy for 2 min. Each condition rule resolves with a silent note.
- **AC-5** (ages checked outside): after each successful backup, the backup container lists the B2 repository (`restic snapshots --host "$RESTIC_HOST" --latest 1 --json`, with the credentials it already holds) and confirms that the newest snapshot is under 26 h old, then pings the healthchecks.io check `backup-verified` (period 24 h, grace 2 h). A failed backup or a failed age check sends no ping, so the silence alerts. On the laptop, after a PASS, `scripts/restore-drill.sh` pings the check `restore-drill` (period 35 days, grace 1 day). A missing ping alerts from healthchecks.io.
- **AC-6** (stuck entry block): when an entry block reason in the alerting set (`stale_feed`, `recording_unhealthy`, `loss_unknown`, `stop_not_accepted`, `exit_failed`, `record_conflict`, `recording_failed`) has been active for `ALERTS_ENTRY_BLOCK_S` (1 h), a warning `entry_block:<reason>` push says so, in the dashboard's plain words. It resolves with a silent note when the reason clears. `halted`, `paused`, `daily_loss_limit`, `trade_cap`, and `loss_cooldown` never raise this alert (you set them, or they already raised their own event, or they clear on a known clock).
- **AC-7** (recorder, context, keys, login): each code in the *Rule table* below produces its listed tier. That covers the promises in specs 0001, 0003, 0004, 0007, 0009, and 0012: for example a `book_gap` with `duration_s` over 60, `context_feed_down` open for 5 min, a missing context hour, `book_sequence_hole`, `kraken_auth_failed`, and 5 `login_failed` lines within any 10 min (counted across all clients, a sliding window).
- **AC-8** (fills): in `live`, each `fill_seen` line produces one silent info push ("Bought 0.0010 BTC at 67,412.5, fee 0.02 USD"). In `demo`, fills push nothing.
- **AC-9** (one push per episode): a repeat of an open episode's fingerprint bumps `occurrences` and `last_seen_at` and pushes nothing. While a warning episode stays open, it pushes again every `ALERTS_RENOTIFY_S` (1 h) with its count. A critical episode is pushed once, and Pushover repeats it every 60 s until you acknowledge it or 1 h passes (Pushover's expiry). A critical condition still open after that pushes again every `ALERTS_CRITICAL_RENOTIFY_S` (4 h). An event rule's episode closes quietly after 15 min with no repeat, so a later occurrence opens a new one. After an alerter restart, open episodes load from Postgres, and nothing is pushed twice.
- **AC-10** (storm cap): at most `ALERTS_HOURLY_CAP` (30) warning and info pushes are `sent` per clock hour (UTC). Re-notifications and summaries count; held, muted, dry run, and failed rows don't. The push that would be the 30th is replaced by one warning "alert storm: N alerts held, see the dashboard", and later ones in that hour are stored with `delivery=capped` and never pushed later (the Activity feed shows them). Critical pushes are never capped and don't count.

*Quiet when it should be*

- **AC-11** (deploy window): a window opens on a `host.jsonl` line with `msg` "deploy started" and closes on the `deploys.jsonl` line with the same `sha` and a `started_at` at or after the window's start, or after 15 min, whichever comes first. A second "deploy started" replaces the open window. An alerter that starts in the middle of a deploy (every deploy recreates it) replays no old line, but opens the window from the last "deploy started" line in `host.jsonl` when that line is under 15 min old and `deploys.jsonl` has no matching line yet; the window still ends 15 min after the deploy's start. During the window, warning and info pushes are held (`delivery=held`). When it ends, warnings still open are pushed as one summary; those that cleared are dropped. Critical pushes go through, except `trader_down`, whose episode opens as usual but is held. A normal deploy restart resolves it silently, and it still counts in the daily note's down minutes. If the heartbeat is still missing when the window ends, it is pushed then. The daily note is never held.
- **AC-12** (mute): a mute command for 1, 4, or 12 h holds warning and info pushes (`delivery=muted`) until it expires or an unmute arrives. A new mute replaces the current one; unmute deletes it. When the mute ends, warnings still open are pushed as one summary. Critical is never muted. The daily note is never muted.
- **AC-13** (laptop dry run): with `ALERTS_DELIVERY=log` (the laptop default), every alert is evaluated and stored with `delivery=dry_run`, and the push it would have sent is logged as `alert_dry_run`. Nothing reaches Pushover, and no healthchecks.io ping is sent.

*When something fails*

- **AC-14** (push fails): a Pushover call that fails (network error, timeout of 10 s, HTTP 5xx or 429) is retried at 5, 15, 30, and 60 s. If all fail, the row is marked `delivery=failed`. A retry cycle runs every 60 s and retries up to 20 `failed` rows, oldest critical first. A row that has been failing for 24 h stops retrying (it stays `failed`, and the daily note lists it). While any push is failing, the alerter pings `alerter-alive/fail` instead of the plain ping, so healthchecks.io alerts you (by email and push) that the push path is broken. An HTTP 4xx other than 429 (a bad token) is not retried and fails the same way. So does an accepted push that reaches no device (`status: 1` with `info` "no active devices to send to"), which the retry cycle sends again once a device is logged in.
- **AC-15** (Postgres down): delivery never waits on storage. With Postgres unreachable, dedupe runs from memory, episodes queue in memory (bounded at 1,000, oldest info dropped first), and they are written when Postgres returns. Postgres being unreachable is itself critical (AC-4).

*Surfaces*

- **AC-16** (dashboard): the Overview's Activity feed shows alert episodes from the last 24 h, newest first, next to the control commands, with tier, title, open or resolved, and the delivery state. A "Mute warnings" control (1 h, 4 h, 12 h, Unmute) and a badge "warnings muted until HH:MM" (Amsterdam time) appear on the Overview. The badge comes from the newest executed `alerts_mute` or `alerts_unmute` result on `{env}:control:alerts:results`, which carries `until`. A "Send test alert" button sends one push per tier, each marked as a test. The button's fragment waits up to 10 s for the command's result, then shows "test sent", the failure reason, or "no answer from the alerter".
- **AC-17** (CLI test): `docker compose run --rm alerter alerter test [--tier critical|warning|info]` sends one marked test push per tier (or the one tier given) through the same delivery path, and prints the Pushover response.
- **AC-18** (daily note): at `ALERTS_DAILY_AT` (08:00) in `ALERTS_DAILY_TZ` (Europe/Amsterdam), one silent push reports: alerts opened in the last 24 h by tier, alerts still open, rows still `failed`, minutes the trader was down in the last 24 h, closed trades and realized P&L for yesterday (UTC day), and the backup container's health. If the alerter was down at 08:00, it sends the note once on start the same day. Its `alerts` row (rule `daily`, fingerprint `daily:<local date>` in `ALERTS_DAILY_TZ`) is the sent marker: before sending, the alerter looks for that row in this `env` and sends only if none exists, whatever the row's delivery (`sent`, `dry_run`, `failed`). The note is never muted, held, or capped, and never counts toward the cap. The note needs Postgres for its numbers and its marker, so while Postgres is unreachable it waits, retrying each minute, and sends once Postgres answers, if it is still the same local day; otherwise that day has no note.
- **AC-19** (rule coverage): a unit test fails when a code promised by an earlier spec (the list in rationale.md, *Promise inventory*) has no rule, or a rule names a code no file in the repo logs.
- **AC-20** (secrets): the Pushover app token, the Pushover user key, and the healthchecks.io ping URLs are secret files under `/run/secrets`, loaded with `load_required`, granted only to the service that uses them (alerter, or backup for `backup-verified`), and redacted from every log line. A missing secret file stops the alerter at start, in `pushover` mode only.

## Decision

**Chosen option**: Option 2: one alerter service that reads logs and Redis, delivering through Pushover, with healthchecks.io as the outside watchdog.

A new `apps/alerter` process evaluates a typed, pure rule table from `trading_core/alerts/` against container logs (through a read only Docker socket proxy), host log lines, Redis snapshots, and host disk space; it stores each episode in Postgres `alerts` and pushes through Pushover. healthchecks.io catches the silence the alerter can't report itself.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

### How an alert flows

```text
container JSON logs ──(socket proxy, read only)──┐
host.jsonl + deploys.jsonl (read only mount) ────┤
Redis snapshots (heartbeat, trader_state) ───────┼─► Observation ─► rules (pure, core) ─► Episode decision
Docker container states (socket proxy) ──────────┤                                           │
disk free (statvfs) / own Postgres, Redis checks ┘                                           ▼
                                                     mute / deploy window / storm cap ─► Delivery
                                                                                      (Pushover | log)
                                                                                             │
                                                                       alerts row (Postgres, or memory queue)
alerter loop healthy ──► healthchecks.io `alerter-alive` (or `/fail` while pushes fail)
```

The pure core turns an `Observation` (source, service, time, code, level, fields) plus the open episodes into a list of decisions (open, bump, resolve, close quietly, push). It does no I/O. The app holds the readers, the delivery client, and the repository.

### Code placement

| Where | What |
|---|---|
| `packages/core/src/trading_core/alerts/` (pure) | `rules.py` (the rule table), `model.py` (`Observation`, `Episode`, `Tier`, `Delivery`), `engine.py` (the episode state machine, dedupe, re-notify, cap, mute and deploy hold), `messages.py` (titles and bodies), `promises.py` (the promised code list for AC-19) |
| `packages/core/src/trading_core/recording/` | `alerts` model in `models.py`; `AlertRepository` writes (insert episode, bump, resolve, mark delivery) |
| `packages/core/src/trading_core/control/` | `AlertCmd` enum and the `{env}:control:alerts` stream helpers, reusing `ControlMessage`'s field format |
| `apps/alerter/` | `main.py` (boot, task supervision), `sources/` (`docker_logs.py`, `host_files.py`, `redis_state.py`, `containers.py`, `disk.py`), `pushover.py`, `watchdog.py`, `commands.py`, `daily.py`, `cli.py` (`alerter test`) |
| `apps/trader/` + `trading_core/node/control.py` | `entries_blocked_since` in `trader_state` |
| `apps/dashboard/` | Activity feed rows from `alerts`, mute control and badge, test button |
| `deploy/host/lib.sh` | `kfs_log` also appends its line to `$KFS_LOG_DIR/host.jsonl` |
| `docker/backup/run.sh` | the verified snapshot check and the `backup-verified` ping |
| `scripts/restore-drill.sh` | the `restore-drill` ping after a PASS |
| `docker/redis/start.sh` | the `alerter` ACL user; the dashboard user gains the alert command stream |

The alerter imports `trading_core` without the `node` extra (no NautilusTrader).

### Rule table

Rule kinds:
- **condition**: opens when a check fails, resolves with a silent note when it passes.
- **event**: opens on a line, closes quietly after 15 min without a repeat.
- **pair**: opens on one code and resolves silently on its partner code from the same service. It also resolves silently when that service restarts (its container start time moves past `opened_at`), or after 24 h with no repeat.

`min_open_s` holds the push until the episode has been open that long (the threshold the condition must last). An episode that resolves earlier is stored and never pushed. It is `0` unless the table says otherwise.

**How lines are read.**
- **Container lines** are JSON with `level`, `msg`, and fields; rules match `code` (plus `event_key`, `level`, or a field where the table says so).
- **Host lines** in `host.jsonl` (`ts`, `level`, `process`, `msg`, plus fields) have no `code`, so rules match `msg`.
- **`deploys.jsonl` lines** (`sha`, `previous_sha`, `started_at`, `finished_at`, `outcome`) match on `outcome`.
- **Precedence.** A line can match several rules: the first match in table order wins, and the rest are ignored.

| Rule | Tier | Kind | `min_open_s` | Matches | Fingerprint |
|---|---|---|---|---|---|
| `trader_down` | critical | condition | 0 | heartbeat missing or older than 45 s | `trader_down` |
| `loss_limit_halt` | critical | event | 0 | trader `code=loss_limit_halt` | `halt:<halt_id>` |
| `risk_critical` | critical | event | 0 | trader line, `level=CRITICAL`, has `event_key` | `event_key` |
| `risk_halt_manual` | info | event | 0 | trader `code=halt`, `level=WARNING` | `event_key` |
| `trader_critical_codes` | critical | event | 0 | trader `code` in `positions_abandoned`, `kill_switch_task_crashed`, `halt_key_invalid`, `record_conflict` (logged at ERROR with no `event_key`) | code |
| `autoheal_gave_up` | critical | event | 0 | host `msg` names autoheal giving up (the exact text `kfs-autoheal` logs at CRITICAL) | `autoheal_gave_up:<container>` |
| `rollback_failed` | critical | event | 0 | `deploys.jsonl` `outcome=rollback_failed` (the host "rollback failed" line is ignored, so one push per rollback) | `rollback_failed:<sha>` |
| `disk_low` | critical | condition | 0 | statvfs free under 5 GB | `disk_low` |
| `catalog_disk_full` | critical | pair (`catalog_disk_ok`) | 0 | recorder `code` | code |
| `recorder_write_failed` | critical | event | 0 | recorder `code` | code |
| `postgres_unreachable`, `redis_unreachable` | critical | condition | 60 | the alerter's own check fails | rule |
| `service_down` | critical | condition | 120 | a core service (`trader`, `recorder`, `postgres`, `redis`, `clock`) has no running container | `service_down:<service>` |
| `key_and_secret_codes` | critical | event | 0 | `code` in `kraken_auth_failed`, `secret_unavailable`, `kraken_key_check_crashed` | `<code>:<service>` |
| `container_unhealthy` | warning | condition | 120 | Docker health `unhealthy` | `container_unhealthy:<service>` |
| `backup_failed` | warning | event | 0 | backup `code` in `backup_failed`, `restic_repo_missing` | `backup_failed:<step>` (`step` field, `repo` for `restic_repo_missing`) |
| `entry_block` | warning | condition | 3600 | AC-6 | `entry_block:<reason>` |
| `context_feed_down` | warning | pair (`context_feed_up`) | 300 | trader or recorder `code` | `context_feed_down:<service>` |
| `context_hour_missing` | warning | event | 0 | compactor `code` (new line, Build plan 3) | `context_hour_missing:<hour>` |
| `book_gap_long` | warning | event | 0 | compactor `code=book_gap`, `duration_s` > 60 | `book_gap_long:<kind>` (one push per gap kind, so a recorder restart that gaps every instrument is one push) |
| `book_sequence_hole` | warning | event | 0 | recorder `code` | code (the recorder keeps one sequence watch per feed, so the line names no instrument; the push names `missing`, and the row's `details` also keeps `run`) |
| `recorder_codes` | warning | event | 0 | `code` in `catalog_retention_blocked`, `catalog_late_rows`, `recorder_queue_full`, `recorder_stop_queue_full`, `compactor_pass_failed`, `recorder_writer_busy`, `recorder_flush_slow` | code |
| `kraken_key_rotation_due` | warning | event | 0 | `code` | code |
| `login_failed_burst` | warning | event | 0 | 5 dashboard `login_failed` lines within any 10 min, all clients counted together | `login_failed_burst` |
| `snapshot_write_failed` | warning | event | 0 | trader `code` | code |
| `engine_error` | warning | event | 0 | trader or recorder line that is not JSON and contains `[ERROR]`; only that line is used (traceback lines that follow are ignored) | `engine_error:<service>` |
| `stale_feed` | warning | event | 0 | trader `code=stale_feed` | `stale_feed` |
| `trade_cap` | info | event | 0 | `event_key` starting `trade_cap:` | `event_key` |
| `fill` | info | event, live only | 0 | trader `code=fill_seen` | `fill:<trade_id>` |
| `alert_storm` | warning | internal | 0 | AC-10 | `alert_storm:<hour>` |
| `summary` | warning | internal | 0 | AC-11, AC-12 | `summary:<window id>` |
| `test` | each tier | internal | 0 | AC-16, AC-17 | `test:<command id>:<tier>` |
| `daily` | info | internal | 0 | AC-18 | `daily:<local date>` (`YYYY-MM-DD` in `ALERTS_DAILY_TZ`) |

A loss limit halt logs both `loss_limit_halt` and a critical `halt:<halt_id>` event. Both share the fingerprint `halt:<halt_id>`, so they make one push.

**Replay safety.** The engine keeps an in memory set of the line keys `(service, docker timestamp, sha256 of the line)` it has seen in the last 30 min, and drops any line older than its service's cursor. A cursor that falls back 5 min, or a reader reattaching after a container restart, can't push a closed event twice.

**Summaries.** A summary (end of a deploy window or a mute) is one warning push titled "N warnings while muted" or "N warnings during deploy". It lists up to 10 titles, then "and N more". It counts toward the cap. Each row it covers becomes `delivery=sent` with `sends + 1`.

**Daily note row.** The `daily` episode is stored resolved at once (`resolved_at = opened_at`), like a test, so it never shows as still open. Its source is `self` and its `details` is `{}`. A failed push is retried by the AC-14 cycle like any row, and the existing row keeps the note from being sent twice. There is no unique index for it: there is one alerter, and it checks for the row before it inserts.

**Pushover fields.** Critical uses priority `2`, `retry=60`, `expire=3600`, and sound `persistent`. Warning uses priority `0`. Info uses priority `-1` ("silent": it shows in the list and on the lock screen, with no sound or vibration). The title is cut to 250 characters and the message to 1,024, with "…" at the end.

**Data model sketch**:

`alerts` (new; one row per episode; kept forever)

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | bigint identity | no | PK |
| `env` | text | no | CHECK `env IN ('demo', 'live')` |
| `rule` | text | no | a rule id from `rules.py` |
| `fingerprint` | text | no | rule plus subject, from the *Rule table* |
| `severity` | text | no | CHECK `IN ('info', 'warning', 'critical')` |
| `source` | text | no | `log:<service>`, `host`, `deploy`, `redis`, `docker`, `disk`, `self` |
| `title` | text | no | redacted, as pushed |
| `message` | text | no | redacted, as pushed |
| `details` | jsonb | no | the first matching line's fields, redacted; `{}` for internal rules |
| `opened_at` | timestamptz | no | UTC |
| `last_seen_at` | timestamptz | no | UTC |
| `resolved_at` | timestamptz | yes | null while open |
| `occurrences` | integer | no | default 1, CHECK `>= 1` |
| `sends` | integer | no | default 0, CHECK `>= 0` |
| `last_sent_at` | timestamptz | yes | |
| `delivery` | text | no | CHECK `IN ('pending', 'sent', 'failed', 'dry_run', 'muted', 'held', 'capped')` |

- Partial unique index `uq_alerts_open` on `(env, fingerprint) WHERE resolved_at IS NULL`: one open episode per fingerprint.
- Index `ix_alerts_env_opened_at` on `(env, opened_at)`.
- No foreign keys; `details` carries `order_id`, `position_id`, or `trade_id` as text.
- Written only through `AlertRepository` (env checked, idempotent on the open fingerprint).

Redis keys (all with a TTL, all safe to lose):

| Key | Value | TTL | Written by |
|---|---|---|---|
| `{env}:alerts:mute` | `{"until": ISO UTC, "command_id": uuid}` | the mute length | alerter, on a mute command |
| `{env}:alerts:cursor:<service>` | the last Docker log timestamp read (RFC 3339, nanoseconds) | 7 days | alerter, every read |

Losing a cursor restarts that service's read 5 min back; the open episode index absorbs the overlap.

**State transitions**:

```text
(none) ──open──► open ──repeat──► open (occurrences + 1)
                  │  └─renotify (warning 1 h, critical condition 4 h)─► open (sends + 1)
                  ├─check passes / partner code / service restart / 24 h quiet pair──► resolved (silent note if it was pushed)
                  └─event rule, 15 min quiet──────► resolved (no note)
delivery: pending ─► sent | dry_run | failed ─(retry ok, within 24 h)─► sent
          pending ─► held (deploy) | muted ─(window ends, still open)─► sent (in a summary)
          pending ─► capped (final)
```

`delivery` records the latest outcome for the episode; `sends` and `last_sent_at` record how many pushes it has had. A row still under its `min_open_s` stays `pending`.

**API surface** (commands, CLI, external calls):

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `{env}:control:alerts` stream, cmd `alerts_mute` | XADD | `id`, `cmd`, `issued_at`, `expires_at`, `schema_version`, `source`, `mute_s` (3600, 14400, 43200) | result on `{env}:control:alerts:results`: `executed` with `until` (ISO UTC) | dashboard Redis user (behind the dashboard login) | `failed invalid` for another `mute_s`; `expired` |
| same, cmd `alerts_unmute` | XADD | as above, no `mute_s` | `executed` with no `until` | same | `expired` |
| same, cmd `alerts_test` | XADD | as above | `executed` once the 3 test pushes are sent or logged | same | `failed push_failed` |
| dashboard `POST /alerts/mute` | POST | `hours`: 1, 4, 12, or 0 for unmute | HTMX fragment with the badge | session + CSRF (spec 0004) | 422 other hours |
| dashboard `POST /alerts/test` | POST | none | HTMX fragment "test sent" or the failure | session + CSRF | 503 when Redis is down |
| `alerter test [--tier T]` | CLI | tier | Pushover response per tier | shell access | exit 1 on a failed push |
| Pushover `POST https://api.pushover.net/1/messages.json` | POST | `token`, `user`, `title`, `message`, `priority` (critical 2 with `retry=60`, `expire=3600`; warning 0; info -1), `timestamp` | `status`, `request`, `receipt` (priority 2) | app token | 4xx bad token or user, 429, 5xx |
| healthchecks.io ping | GET | `<check url>` or `<check url>/fail` | 200 | the secret URL | network error (logged, retried next minute) |

The alerter consumes `{env}:control:alerts` in its own consumer group `alerter` and answers on `{env}:control:alerts:results`. The trader never reads this stream, and the alerter never reads `{env}:control`.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| `trader_down` | heartbeat age | `now` minus `ts_ns` of `{env}:snapshot:heartbeat` (envelope, `trading_core.snapshot`); a missing key counts as older than 45 s |
| `trader_down` resolved note | minutes down | `resolved_at` minus `opened_at` of the episode |
| risk alerts | title and body | the line's `msg` and `event_key`; the `kind` is the line's `code` |
| other code rule alerts (for example `record_conflict`, `book_sequence_hole`) | title and body | title "<code in plain words> on <service>"; body the line's `msg`, then each of `event_key`, `reason`, `step`, `kind`, `duration_s`, `missing`, `table`, `error`, `secret` the line carries (`messages._BODY_FIELDS`); every other field stays only in the row's `details` |
| `fill` push | side, quantity, instrument, price, fee | the `fills` row with this `env` and `trade_id` (from the `fill_seen` line), using its side, quantity, price, commission, and instrument columns as spec 0002 defines them; quantity and price shown with the instrument's precision, fee in its currency; looked up every 2 s for up to 10 s, then "fill <trade_id>" |
| `entry_block` | reason and since | `trader_state.data.entries_blocked` and the new `entries_blocked_since` (reason to UTC ns); plain words from `strategy.blocks.PLAIN_WORDS` |
| `entry_block` across a trader restart | since | the earlier of the stored `opened_at` of a pending episode for that reason and the reported since, when the reason was seen less than 5 min before |
| `disk_low` | free GB | `os.statvfs` on the mounted `/host/kfs-log` (the host root filesystem, which also holds `/var/lib/docker`) |
| `book_gap_long` | gap length | the line's `duration_s` |
| `container_unhealthy`, `service_down` | state and how long | Docker `GET /containers/json` through the proxy: `State`, `Status`, the `com.docker.compose.service` label |
| deploy window | start and end | `host.jsonl` line `msg="deploy started"` (`sha`), ended by the next line in `deploys.jsonl` with that `sha` or 15 min |
| `rollback_failed` | sha | `deploys.jsonl` outcome line |
| mute badge | until | `until` of the newest executed mute or unmute result on `{env}:control:alerts:results` (the dashboard has no read on `*:alerts:*`), shown in `Europe/Amsterdam`; hidden once past |
| mute in the engine | until | `{env}:alerts:mute` `until`, set by the alerter with that TTL, deleted on unmute |
| daily note | counts by tier, still open, still failed | `alerts` rows with `opened_at` in the last 24 h, leaving out `rule='daily'` (test rows still count); rows with `resolved_at IS NULL`; rows with `delivery='failed'` |
| daily note | sent today | an `alerts` row of this `env` with `rule='daily'` and fingerprint `daily:<local date>`, the date taken in `ALERTS_DAILY_TZ` |
| daily note | trader down minutes | for each `trader_down` episode overlapping the last 24 h: `min(resolved_at or now, now) − max(opened_at, now − 24 h)`, summed, whatever its delivery |
| daily note | trades and realized P&L yesterday | `positions` of this `env` with `closed_at` in yesterday's UTC day: count and sum of `realized_pnl` (Decimal, net of fees as stored, spec 0002); "no trades" when the count is 0 |
| daily note | backup health | the backup container's Docker health (healthy means a backup in the last 26 h, spec 0005) |
| daily note | when to send | `ALERTS_DAILY_AT` in `ALERTS_DAILY_TZ`, converted to UTC per day (handles summer time) |
| every push | env tag | `TRADING_ENV`; the title starts with `[demo]` or `[live]` |
| Pushover call | token, user | secret files `pushover_app_token`, `pushover_user_key` |
| pings | URLs | secret files `hc_alerter_url` (alerter), `hc_backup_url` (backup), `./secrets/hc_restore_drill_url` (laptop, optional) |

**Key invariants**:
- At most one open episode per `(env, fingerprint)` (DB partial unique index; the engine checks it first).
- A critical push, a test push, and the daily note are never muted or capped. Of these, only `trader_down` is held, and only during a deploy window.
- Delivery never waits on Postgres (AC-15).
- Nothing is pushed from `ALERTS_DELIVERY=log`, and no ping is sent from it.
- Every stored or pushed text has passed `trading_core.secrets.redact`.
- The alerter never writes to `{env}:control`, never imports NautilusTrader, and never places, cancels, or changes an order.
- All times are UTC in storage; only the daily note's schedule and the dashboard's badge are shown in Amsterdam time.

**Security model**:
- **Docker access** goes only through a socket proxy (`tecnativa/docker-socket-proxy`, pinned to an exact version tag) with `CONTAINERS=1` and every other section, including `POST`, at 0. It sits on an internal network `dockerproxy` shared only with the alerter. The proxy mounts the socket read only. This exposes container listings, inspect, and logs; our secrets are files, never env vars (spec 0003), so inspect shows none.
- **Host files**: `/var/log/kfs` mounts read only into the alerter at `/host/kfs-log` (VPS only, in `compose.vps.yaml`).
- **Postgres**: a new `alerter_app` role (password secret `postgres_alerter_password`) with SELECT, INSERT, UPDATE on `alerts` and SELECT on `fills` and `positions`. `dashboard_app` gains SELECT on `alerts`.
- **Redis**: a new ACL user `alerter` (password secret `redis_alerter_password`): read `*:snapshot:*`; read and write `*:alerts:*`, `*:control:alerts`, and `*:control:alerts:results`; stream, string, and key commands only, no admin. The `dashboard` user gains XADD on `*:control:alerts` and read on `*:control:alerts:results`.
- **Commands** reach the alerter only through the dashboard, behind its password and authenticator login (spec 0004). A mute can only hold warnings, so a stolen session can't silence a critical alert.
- **Push content** carries no secret, no key fingerprint, and no account balance beyond the daily realized P&L. Pushover sees the message text; that is the accepted third party exposure.

**Configuration required**:
- `ALERTS_DELIVERY`: `log` or `pushover`. Default `log`; the VPS sets `pushover` in `/etc/kfs/kfs.env` (no default in `compose.vps.yaml`, so a missing line refuses the deploy rather than silently running dry).
- `ALERTS_TRADER_DOWN_S`: heartbeat age for `trader_down`, default 45.
- `ALERTS_HOURLY_CAP`: warning and info pushes per hour, default 30.
- `ALERTS_RENOTIFY_S`: re-notify interval for open warnings, default 3600.
- `ALERTS_CRITICAL_RENOTIFY_S`: re-push interval for a critical condition still open after Pushover's 1 h expiry, default 14400.
- `ALERTS_ENTRY_BLOCK_S`: entry block age, default 3600.
- `ALERTS_DISK_FREE_GB`: host free space floor, default 5.
- `ALERTS_DAILY_AT`: `HH:MM`, default `08:00`.
- `ALERTS_DAILY_TZ`: IANA zone, default `Europe/Amsterdam`.
- Secrets (files, the four step recipe): `pushover_app_token`, `pushover_user_key`, `hc_alerter_url`, `hc_backup_url`, `postgres_alerter_password`, `redis_alerter_password`. Laptop only and optional: `./secrets/hc_restore_drill_url`.
- Accounts to create first: a Pushover account and application (iOS app, one time purchase), and a healthchecks.io account with the three checks and its Pushover integration plus email.

**Critical test scenarios**:
- Happy path: stop the trader on the laptop with `ALERTS_DELIVERY=log`; within 60 s an `alerts` row `trader_down` opens with `delivery=dry_run`; start it, and the row resolves. Verifies **AC-1**, **AC-13**.
- Happy path (VPS): `alerter test` produces three pushes, the critical one sounds through Do Not Disturb. Verifies **AC-17**.
- Dedupe: feed the engine 100 `kraken_auth_failed` observations 30 s apart; one push, `occurrences=100`, one re-notify per hour. Restart the engine from the stored open episodes; no new push. Verifies **AC-9**.
- Storm: 40 warning observations with distinct fingerprints in one hour; 29 pushes, 1 storm push, 10 `capped`; a critical in the same hour still pushes. Verifies **AC-10**.
- Failure case: Pushover returns 503; four retries, `delivery=failed`, the watchdog ping becomes `/fail`; Pushover recovers and the next cycle sends it. Verifies **AC-14**.
- Failure case: Postgres stopped; a critical event still pushes; rows appear when Postgres returns. Verifies **AC-15**.
- Deploy window: a warning during a deploy is held and dropped if it resolved; one still open is sent as a summary after; a critical goes straight through. Verifies **AC-11**.
- Auth/permission: the dashboard Redis user can't write `{env}:alerts:mute` directly (ACL denies); an `alerts_mute` with `mute_s=999999` is answered `failed invalid`; a critical during a mute still pushes. Verifies **AC-12**, **AC-20**.
- Coverage: remove the `book_sequence_hole` rule, and the coverage test fails. Verifies **AC-19**.
- Daily note: at 08:00 local the engine sends one note and stores `daily:<date>`; a restart the same day sends nothing; a start after 08:00 with no row sends once; during a mute or a deploy window it still sends; with Postgres down it waits, then sends once. Verifies **AC-18**.

## Build plan

Tracer Bullet: the first slice is one real thread from a real signal (the trader's heartbeat) to a real push and a stored row, then each source is added end to end.

1. **Thin thread: heartbeat to phone**
   1. Core `trading_core/alerts/` pure model and engine with the `trader_down`, `test`, and internal rules; unit tests for open, resolve, re-notify, restart load. Satisfies **AC-1**, **AC-9**. (built)
   2. Migration: `alerts` table and indexes, `alerter_app` role and grants (`db_roles.py`), `dashboard_app` SELECT; `AlertRepository` with integration tests. Satisfies **AC-9**, **AC-15**. (built)
   3. Secrets and settings via the recipes: the six secret files, the `ALERTS_*` settings class, `dev-secrets.sh`, `.env.example`, `deploy/kfs.env.example`; the `alerter` Redis ACL user in `docker/redis/start.sh`. Satisfies **AC-20**. (built; `hc_backup_url` is declared in Compose with its first user, task 3.3)
   4. `apps/alerter`: boot, Redis heartbeat source, Pushover client with retries, `log` delivery, `alerter-alive` ping and `/fail`, `alerter test` CLI; Compose service with healthcheck (heartbeat file, as the node anchor). Satisfies **AC-1**, **AC-2**, **AC-13**, **AC-14**, **AC-17**. (built)
   5. Laptop end to end in dry run, then deploy and run verify steps 1 to 4. Satisfies **AC-1**, **AC-2**, **AC-17**. (laptop half done 2026-10-16: Part A steps 1 to 3 passed; the deploy and Part B steps 1 to 4 wait for the accounts)
2. **Container logs**
   1. Docker socket proxy service and `dockerproxy` network. `docker_logs.py` polls each service's logs every 5 s with `since=<cursor>&timestamps=1`. It finds containers by the Compose project label, so other containers on the host are ignored, and one shot services (`migrate`, `tools`, `lab`, `dashboard-credentials`) are skipped. It also keeps the Redis cursors and the replay set. `containers.py` polls states every 15 s. Satisfies **AC-3**, **AC-4**. (built)
   2. The full rule table for log codes, `engine_error`, `service_down`, `container_unhealthy`, `login_failed_burst`; `promises.py` and the coverage test. Satisfies **AC-3**, **AC-4**, **AC-7**, **AC-19**. (built)
   3. The storm cap and the Postgres down memory queue, with `postgres_unreachable` and `redis_unreachable`. Satisfies **AC-4**, **AC-10**, **AC-15**. (built)
   4. Tidy after the gap decisions (settled 2026-10-16, rationale *Settled after the build*): in `trading_core/alerts/rules.py`, give `BOOK_SEQUENCE_HOLE` the default code key (drop the `instrument_id` lookup and its comment) and point the `record_conflict` comment at this spec's `trader_critical_codes` row; in `trading_core/alerts/promises.py`, remove the `context_feed_lagging` promise and its `PENDING` entry; in `trading_core/alerts/messages.py`, add `table` to `_BODY_FIELDS` after `missing`, with a test that a `record_conflict` push body names its table. Satisfies **AC-3**, **AC-7**, **AC-19**. (built)
3. **Host, backup, and the trader's block age**
   1. `kfs_log` appends to `$KFS_LOG_DIR/host.jsonl` (created `0644`, rotated by a logrotate snippet at 10 MB × 5 with `copytruncate`); `host_files.py` follows `host.jsonl` and `deploys.jsonl`, tolerating truncation; the deploy window. Satisfies **AC-4**, **AC-11**. (built)
   2. `disk.py` (statvfs every 60 s) and the `compose.vps.yaml` read only mount. Satisfies **AC-4**. (built)
   3. `docker/backup/run.sh`: after a successful run, list `--host "$RESTIC_HOST" --latest 1`, check the age is under 26 h, then ping `hc_backup_url` with `curl -fsS -m 10 --retry 3` (add `curl` to the backup image if missing). `scripts/restore-drill.sh`: after a PASS, ping `./secrets/hc_restore_drill_url` the same way when the file exists, else print a reminder. The alerter's pings use the same timeout and retries. Satisfies **AC-5**. (built)
   4. Trader: `entries_blocked_since` (reason to UTC ns, set when a reason is added, removed when it clears) in `trader_state`; the alerter's `entry_block` rule with the restart rule. Satisfies **AC-6**. (built)
   5. Compactor: log `context_hour_missing` (WARNING, `hour`) when an hour closes with no context file for a feed that was up (spec 0009). Satisfies **AC-7**. (built)
4. **Phone polish and the dashboard**
   1. Live fill pushes with the `fills` lookup. Satisfies **AC-8**. (built)
   2. Alert command stream: `AlertCmd`, the `alerter` consumer group, mute and unmute with the summary on expiry, `alerts_test`; dashboard Redis ACL change. Satisfies **AC-12**, **AC-16**. (built)
   3. Dashboard: Activity feed rows from `alerts`, mute control and badge, test button; styleguide samples. Satisfies **AC-16**. (built)
   4. Daily note with its catch up on start, the `daily` rule, and a `PushReason.DAILY` that passes the hold and the cap as `TEST` does (`engine._holding`, `engine._gate`). Satisfies **AC-18**. (built)
   5. Full verify on the VPS (verify.md), including a real night time critical. Satisfies **AC-1** to **AC-20**.

## Consequences

**Positive**:
- Every alert promise left by nine specs lands in one tested table, and the coverage test keeps future codes from being forgotten.
- Producers barely change: the codes they already log become alerts, so the trader's order path is untouched.
- A dead VPS, a dead alerter, a missing backup, and an old drill are all caught from outside, for free.
- The dashboard and the phone share one record of what was sent.

**Negative / tradeoffs**:
- Alerts are coupled to log codes and messages. Renaming a code silently drops an alert unless the coverage test catches it (it does for promised codes, not for new ones).
- The socket proxy is a new component with access to container metadata and logs. It is read only, but it is still the most privileged thing the alerter touches.
- Pushover is a paid (one time) third party that sees alert text. If Pushover is down, only the healthchecks.io email path remains.
- Detection of a whole VPS loss takes up to 3 min, not 1 (the healthchecks.io floor).
- `engine_error` matches plain text, so a Nautilus log format change can break or flood it; the per service dedupe limits the flood.
- An entry block reason that clears and comes back within 5 min keeps its first since time; one that stays away longer starts over.

**Neutral**:
- A fifth app (`apps/alerter`) and a sixth container (the proxy) in Compose; one migration; two new roles (Postgres, Redis).
- `AGENTS.md`'s line "the dashboard … writes only to the `control` stream" becomes "the control streams" (`{env}:control` and `{env}:control:alerts`); /sync owns that edit.
- New `/etc/kfs/kfs.env` lines must be added by hand before the deploy, as with every no default setting.

## Follow-up

- [ ] Create the Pushover account and application, buy the iOS app, enable Critical Alerts for Pushover in iOS Settings, and place the token and user key with `place-secrets.sh`.
- [ ] Create the healthchecks.io account, its Pushover and email integrations, and the checks `alerter-alive` (1 min, grace 2 min), `backup-verified` (24 h, grace 2 h), `restore-drill` (35 days, grace 1 day).
- [ ] /sync: add `apps/alerter/AGENTS.md`, point to it from root `AGENTS.md`, widen the dashboard rule to "the control streams", and record under `## Agent skills` `Declined:` the Pushover, healthchecks.io, and Docker socket proxy skills (declined on 2026-10-16).
- [ ] Kraken's scheduled maintenance (status.kraken.com) could open a mute window by itself; for now you mute by hand.
- [ ] Spec 0001's deferred Nautilus JSON log sink would let `engine_error` match fields instead of text; revisit if it proves noisy.
