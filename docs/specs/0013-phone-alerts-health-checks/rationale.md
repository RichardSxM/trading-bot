# 0013. Rationale: phone alerts & health checks

## Context

The bot runs around the clock on a London VPS. You are in the Netherlands and are often asleep or away from the dashboard, which is reachable only over Tailscale. Today nothing pushes to your phone. What the code calls an "alert" is a JSON log line, sometimes with a `risk_events` row, plus the dashboard's on page alert bar. So a stop that Kraken refuses at 03:00, a failing backup, or a dead server is seen only when you next open the dashboard.

Nine earlier specs left work for this feature: 0001, 0003, 0004, 0005, 0006, 0007, 0009, 0010, and 0012. Together they name about forty codes and conditions to alert on (see *Promise inventory*). Most already exist as log codes, emitted by the trader, recorder, compactor, dashboard, backup, and the host scripts. Four don't exist yet:
- an entry block's start time
- a record of the restore drill's age
- a check of the newest B2 snapshot from outside the VPS
- a host disk check under 5 GB

Some failures can't report themselves: a crashed trader, a dead Docker daemon, a VPS that lost power. They need something outside the VPS that notices silence.

The forces at play:
- **Urgency.** You want to know within a minute, and the worst cases must wake you through Do Not Disturb on an iPhone.
- **Noise.** A restart loop logs the same line every 30 s, and a scalper can fill many times an hour. Without dedupe, the alerts would be ignored within a week.
- **Rules.** The project's rules hold too: secrets only as files, fail closed on orders, no floats for money, UTC everywhere, the dashboard writing only through control streams, and Clean Architecture with pure rules in core.
- **Size.** One person operates this, so every new moving part costs real attention.

If this is left undecided, the kill switch, loss limits, and recorder health checks protect the account but stay silent. A fault is found hours later, and spec 0010's "Stop failed" bar remains the only warning.

## Options considered

### Option 1: Producers publish alerts to a Redis stream

Each process calls a core `raise_alert(...)` at the point of failure. That writes to an `{env}:alerts` stream, and a small sender service delivers from it. Host scripts would write to the stream with `redis-cli`.

**Pros**:
- Explicit: an alert is a deliberate call with typed fields, not an inferred log line.
- No log parsing and no Docker socket access.

**Cons**:
- Every producer changes, including the trader's order path, for about forty call sites.
- A process that crashes, hangs, or never starts can't publish its own failure, which is exactly the case that matters most.
- Host scripts need Redis credentials, and Redis being down silences every alert.
- Two sources of truth: the log line and the alert call can drift apart.

### Option 2: One alerter that reads logs, Redis, and Docker state (chosen)

A dedicated `alerter` service reads every container's JSON logs through a read only Docker socket proxy. It also follows the host's JSON lines and checks the Redis snapshots, container states, and disk space. A typed rule table in core maps codes and conditions to tiered alerts. healthchecks.io watches the alerter, the backup, and the drill from outside.

**Pros**:
- The promises in earlier specs are already written as "alert on code X", so the rule table matches them one to one.
- Producers barely change. Only three small additions are needed: a block start time, a host log file, and a compactor line.
- It sees processes that died, because it reads their last lines and their container state from outside them.
- One place holds dedupe, muting, the cap, and storage.

**Cons**:
- It depends on log codes staying stable; a rename drops an alert unless a test catches it.
- It adds a Docker socket proxy, a privileged read path to container metadata.
- Plain text NautilusTrader lines can only be matched loosely.

### Option 3: Switch Compose to the journald log driver and read one journal

Every container logs to journald with the host units, and the alerter reads one journal.

**Pros**:
- One stream for containers and host units, with no socket access.

**Cons**:
- On Docker Desktop (the laptop) the journal lives inside the VM, so the laptop test path breaks.
- It changes logging for every service and the existing `docker compose logs` habits.
- Reading the host journal from a container ties the image to the host's journal format.

### Option 4: A hosted log and alert platform (Grafana Cloud, Better Stack)

Ship logs to a hosted service and define alerts there.

**Pros**:
- Dashboards, search, and alerting are maintained for you.

**Cons**:
- Every log line leaves the VPS, including trading details, so there is a large third party exposure.
- Free tiers cap volume, and the recorder's logs alone are heavy.
- The alert rules live outside the repo, untested and unreviewed, which breaks the spec driven workflow.
- iPhone delivery through Do Not Disturb still needs Pushover or similar.

### Delivery and watchdog choices (asked one by one)

- **Push service: Pushover** over ntfy.sh and a Telegram bot. Of the three, only Pushover's emergency priority repeats until acknowledged and can use iOS Critical Alerts to sound through Do Not Disturb, which is what you asked for. ntfy.sh is free, and so is Telegram, but neither can break Do Not Disturb on iOS. The cost is a one time app purchase, and Pushover sees the message text.
- **Outside watchdog: healthchecks.io free** over a Cronitor or Better Stack heartbeat and a laptop poller. Push style checks fit a VPS reachable only over Tailscale, because nothing outside can poll in. The free tier covers the three checks needed, and it can deliver through Pushover and email. A laptop poller misses exactly the night time outage.
- **Storage: Postgres `alerts`** over Redis only and no storage. Postgres keeps dedupe correct across restarts, gives the Activity feed a source, and keeps an audit of what was sent.
- **Rules: a typed table in core** over a mounted YAML file. It is type checked and unit tested, and the coverage test needs it in code.
- **B2 age: a ping after a verified backup** over a GitHub Action listing B2. Silence is still detected outside the VPS, and no B2 key leaves the VPS.

## Rationale

The deciding force is that the failures worth waking you for are mostly ones where a process can't speak for itself. A trader that dies, a container stuck unhealthy, a recorder that hangs during Kraken maintenance, a VPS that goes dark: none of these can raise an alert, so Option 1 fails at its core job. Option 2 reads each process's output and state from outside it, and healthchecks.io covers the one thing even the alerter can't see, its own death. That pairing is the smallest design that covers every promise in the inventory.

Option 2 also fits how this project already works. The earlier specs were written against log codes, so a rule table that matches codes is a direct translation, and the coverage test turns "feature 12 will alert on X" from a promise into a check. Keeping the rules pure in core follows the Clean Architecture rule and makes the hardest logic (dedupe, re-notify, cap, mute, deploy hold) testable without Redis, Postgres, or Docker. The socket proxy is the price: it is read only, limited to the containers section, and on an internal network, so it is a contained and well understood risk, smaller than giving the alerter the raw socket or giving every host script Redis credentials.

Two choices give up a little exactness for safety. Delivery never waits on Postgres, because a database outage is one of the things you need to hear about. A mute or a deploy window never holds a critical alert, so no mistake on the dashboard, and no stolen session, can silence the alerts that protect money. The one exception, holding `trader_down` during a deploy, is bounded by the deploy's own 15 minute window and still fires if the trader doesn't come back.

## RECOMMEND items settled while writing

- **Socket proxy**: `tecnativa/docker-socket-proxy`, pinned to an exact tag, `CONTAINERS=1` only. Runner up: mounting `/var/lib/docker/containers` read only, which needs root in the alerter and breaks on Docker Desktop.
- **Alert commands on a sibling stream** `{env}:control:alerts`, consumed by group `alerter`. The trader's consumer answers an unknown `cmd` with `failed invalid`, so sharing `{env}:control` would race two answers per command. Runner up: teach the trader to skip alert commands, which touches the trader's safety code for no benefit.
- **The deploy window from existing log lines** ("deploy started" in `host.jsonl`, the outcome in `deploys.jsonl`) instead of a new Redis key written by `kfs-deploy`. There is no new Redis credential on the host, and a crashed deploy is bounded by the 15 min cap. This replaces the `{env}:alerts:deploy` key named during the design conversation.
- **Polling**: Docker logs are polled every 5 s with `since=<cursor>` (simpler and safer across container restarts than holding a follow stream open), container states every 15 s, Redis every 5 s, disk every 60 s. That is fast enough for the 60 s goal, and cheap. Runner up: the follow stream, which is a little faster but needs reattach logic on every restart.
- **Settled after the cross check** (2026-10-16, applied on your pick): host lines match `msg`; `rollback_failed` is read only from `deploys.jsonl`; context and gap fingerprints use the service and the gap kind; a replay set guards cursor loss; `min_open_s` per rule; pair episodes also resolve on a service restart or after 24 h; a critical condition pushes again every 4 h after Pushover's expiry; cap accounting, summaries, mute replacement, and the failed retry window are defined; the dashboard's mute badge reads the result stream.
- **Tier per code**: money at risk, data loss now, or the alert path itself broken means critical; degraded but self healing means warning; good news or confirmation means info.
- **`login_failed` burst**: 5 in 10 min, enough to catch guessing and quiet on a typo.
- **Restore drill ping URL** lives in `./secrets/hc_restore_drill_url` on the laptop, beside the other untracked laptop secrets, and is optional (the drill prints a reminder without it).
- **`context_hour_missing`** is a new compactor log line; nothing logs it today.
- **Entry block alerting set**: excludes `halted`, `paused`, `daily_loss_limit`, `trade_cap`, and `loss_cooldown`, because you set them, they already raised their own event, or they clear on a known clock.
- **Uptime in the daily note** became "minutes the trader was down in the last 24 h", taken from `trader_down` episodes, because no snapshot carries a start time.

## Promise inventory

Every alert promised before this spec, with the rule that keeps it. `promises.py` holds this list for AC-19.

| Spec | Promise | Rule |
|---|---|---|
| 0001 | alerts and heartbeats belong to feature 12; stops and alerts mitigate venue risk | `trader_down`, `risk_critical` |
| 0001 | catch the loop when no time server answers | `service_down:clock`, `container_unhealthy:clock` |
| 0003 | `kraken_auth_failed` (revoked key restart loop) | `kraken_auth_failed` |
| 0003 | `kraken_key_rotation_due`, `kraken_key_check_crashed`, `secret_unavailable` | same names |
| 0004 | `login_failed` bursts, `snapshot_write_failed` | `login_failed_burst`, `snapshot_write_failed` |
| 0004 | the Activity feed names alert sources | AC-16 |
| 0005 | `autoheal_gave_up`, `backup_failed`, an unhealthy backup, `rollback_failed`, disk under 5 GB | `autoheal_gave_up`, `backup_failed`, `container_unhealthy:backup`, `rollback_failed`, `disk_low` |
| 0005 | restore drill older than 35 days; newest `kfs-vps` B2 snapshot older than 26 h, seen from outside | healthchecks.io `restore-drill`, `backup-verified` |
| 0006 | `stop_not_accepted`, `exit_failed`, abandoned position, `record_conflict` | `risk_critical`, `positions_abandoned` |
| 0006 | an entry block lasting over an hour | `entry_block` |
| 0007 | ERROR and CRITICAL codes; `book_gap` over 60 s | the recorder rows of the rule table, `book_gap_long` |
| 0007 | `catalog_disk_full`, `catalog_retention_blocked`, `catalog_late_rows`, `recorder_queue_full`, `recorder_stop_queue_full`, `compactor_pass_failed`, `backup_failed step=catalog` | same names, `backup_failed:catalog` |
| 0007 | `book_sequence_hole` | `book_sequence_hole` |
| 0009 | `context_feed_down` over 5 min, `context_feed_lagging`, an hour missing a context file | `context_feed_down`, `context_feed_lagging`, `context_hour_missing` |
| 0010 | every critical `halt`, `flatten_timeout`, `halt_key_write_failed` | `risk_critical` |
| 0010 | the "Stop failed" bar is the only warning until now | `risk_critical` (the failure events behind the bar) |
| 0012 | `recorder_write_failed`, `recorder_writer_busy`, `recorder_flush_slow` | same names |
| review 2026-10-11 | a distinct ERROR line for the recorder so feature 12 can page on it | recorder rows of the rule table |
| scope | fills | `fill` (live only) |
