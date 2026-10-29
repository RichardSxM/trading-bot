# 0023. Rebuild the trader's view from Kraken after every restart or outage, fix what is safe to fix, flatten and halt on the rest, and prove it with a live drill before go live

**Date**: 2026-10-29 (amended 2026-10-29: AC-7, AC-8, AC-9, AC-11 after the task 3 build)
**Status**: In Progress

## Summary

After a crash, a restart, or a network loss, the trader now checks its own view of orders and positions against what Kraken really holds before it trades again. It does this at start, after a reconnect, and every 60 seconds. What it can explain it keeps managing: our own position, a stop that needs replacing, a partial fill. What it cannot explain it closes and halts with a phone alert: an unknown position, or a size our fills do not add up to. Every check is saved as a report you can see on the dashboard. A new drill only live mode lets you prove all of this on your real Kraken account with one tiny position before feature 19 risks real capital.

## Requirements

**User stories**:
- As the trader, I want the bot to rebuild its view from Kraken's real orders and positions after any crash, restart, or dropped connection, so it never trades on a picture that is wrong.
- As the trader, I want a position the bot can explain to keep its stop and its strategy across a restart, and anything it cannot explain to be closed and halted with an alert, so a restart never leaves money unmanaged.
- As the trader, I want the bot to never send a duplicate entry, so a lost acknowledgement or a restart cannot double my exposure.
- As the trader, I want to see what the bot wants, what it believes, and what Kraken confirms, side by side, so a submitted flatten never looks like a confirmed flat.
- As the trader, I want to prove all of this on my real account at almost no cost before go live, and have a runbook for when it goes wrong.

**Acceptance criteria**:

*When the check runs*
- **AC-1** (start check): every trader start runs a reconciliation check (a comparison of Kraken's open orders and positions with the trader's own) with trigger `start` before any strategy may enter. Until it finishes `clean`, entries are blocked with the new reason `reconciling`. A clean check clears the block by itself. A latched halt (spec 0010) stays halted.
- **AC-2** (periodic check): every `RECONCILE_INTERVAL_S` (60 s) a check with trigger `periodic` runs. A `recheck` control command (AC-20) runs one at once.
- **AC-3** (reconnect check): when the `stale_feed` block clears (the network is back), a check with trigger `reconnect` runs at once. Entries stay blocked with `reconciling` from the moment `stale_feed` starts until that check is `clean`. Known gap: a drop of only the private socket while the market feed stays up fires no reconnect check. The periodic check bounds it to `RECONCILE_INTERVAL_S`.

*Fail closed*
- **AC-4** (Kraken unreachable): Kraken's view is fetched through a wrapper that raises on any error, timeout, or HTTP failure. It never turns an error into "no orders, no position". A failed fetch ends the check `failed`, sets the entry block `exchange_unknown`, and retries with backoff (`RECONCILE_RETRY_S`, doubling up to 60 s). Exits and the stops resting on Kraken are untouched. The block clears only on the next `clean` check.
- **AC-5** (start can't reach Kraken): when the start check cannot reach Kraken within `RECONCILE_START_TIMEOUT_S` (300 s), the trader exits 1 and Docker restarts it with backoff. While it retries, it touches the heartbeat file on each attempt, so the healthcheck (90 s start period) and autoheal never cut the retry short. When NautilusTrader's own startup reconciliation fails, the kernel never starts the strategies. A startup watchdog exits 1 when the strategies are not running within `RECONCILE_NAUTILUS_TIMEOUT_S` + 30 s after the node is built, so the trader never runs a node whose state was not reconciled.

*What a check decides* (live; the exchange view is Kraken, see AC-23 for demo)
- **AC-6** (known position): a Kraken position is *known* when the Nautilus cache holds an open position on that instrument with the same side and quantity, and every fill in it comes from an order the trader created: an order in the cache whose strategy id is a loaded strategy or the `ReconcileExecutor`, or a kill switch exit. An order Nautilus generated during reconciliation (strategy `EXTERNAL`, or its reconciliation tag; the build confirms the exact marker in 1.231.0) is never ours. A known position is adopted with no mismatch. It covers the partial fill case: an entry partly filled before a crash is known when our own fills explain the whole size. A lost Redis cache leaves only generated fills, so the position is unknown and AC-7 applies.
- **AC-7** (unknown or wrong position): a Kraken position that is not known (no cache position, a different side, or a size our own fills do not explain) is a `unknown_position` or `position_mismatch` finding. The trader latches a halt with the new reason `reconcile_mismatch` through `KillSwitch.halt_automatic`, so the halt key, the halt event, the wait for flat, and the flatten after a halted restart all work as in spec 0010. A new thin `ReconcileExecutor` strategy (loaded in live only, and part of the kill switch's flatten) closes the position with a reduce only market order, because no other strategy owns it and an Actor cannot submit orders. It retries every 1 s like spec 0010's exit loop. Only your Resume clears the halt.
  How the cache is brought in line depends on which side holds more. When Kraken holds more than the cache (or the cache holds nothing), Kraken's position report goes to `exec_engine.reconcile_execution_report`, and Nautilus adds the difference as an `EXTERNAL` position that the `ReconcileExecutor` closes (`cache_fix: synced`). When the cache holds more than Kraken, or the other side, AC-8 applies.
- **AC-8** (cache holds more than Kraken): a cache position larger than Kraken's, on the other side, or with no Kraken position at all, that is still there after the AC-11 fill recovery and the compare that follows it, is a `position_mismatch` with `cache_fix: manual`. Nautilus 1.231.0 cannot shrink a strategy's position from a position report: a netting fill goes to `{instrument}-{strategy_id}`, so a generated `EXTERNAL` fill opens a new position instead. The usual cause, a missed fill of our own order (most often the stop), is therefore fixed by AC-11 before this finding can arise. When recovery leaves the cache holding less than Kraken, AC-7's `synced` path applies instead. On a `cache_fix: manual` finding the trader:
  - adds the instrument to the cache untrusted set (`{env}:reconcile:cache_untrusted`, a Redis set that survives restarts);
  - latches a halt with `reconcile_mismatch` (keeping an existing halt, never a second one);
  - has the `ReconcileExecutor` close what Kraken holds: it reads Kraken's quantity and side from a fresh fetch right before each attempt, sends a reduce only market order on the closing side (which can never open or flip a position), and sends nothing when Kraken is flat;
  - leaves the cache as it is.
  For an untrusted instrument, the kill switch flatten, its re-flatten loop in the wait for flat, and the flatten after a halted restart (spec 0010) all skip the owning strategy's own close, because its cached size is wrong. The wait for flat judges an untrusted instrument flat from a fresh Kraken fetch, not from the cache, so it never times out on the wrong cache. You then fix the cache by hand as the runbook (AC-26) explains: confirm Kraken is flat, stop the trader, run `python -m trader.cache reset` (Interface surface), and start it. The untrusted set clears on that reset, or on a clean start check. Press Resume once the start check is clean.
- **AC-9** (stop check): every known position whose owning strategy uses a stop (`KfsStrategy.uses_stop`, true by default; false for `BuyAndHold` and `RandomTaker`, which never place one, and for the `ReconcileExecutor`, whose positions are always being closed) must have exactly one reduce only stop on Kraken, for the full position size, at the recorded trigger price from the value sourcing table. A missing stop is `missing_stop`, and a wrong size or extra stops is `stop_size_mismatch`. The `ReconcileExecutor` places the target stop if it is missing (tagged `parent:<opening order>`, a sell stop for a long and a buy stop for a short). Only after Kraken accepts it does it cancel every other stop on that position (never naked). The rule is safe to repeat: after a crash between the two steps, the next check finds the target stop and only cancels the rest. If Kraken rejects the target stop, the trader flattens and latches a halt with `reconcile_mismatch`. If the last quote's mid has already crossed the trigger, the trader closes the position at market instead (outcome `mismatch`, no halt: it did what the stop would have done). With no quote younger than `RISK_STALE_QUOTE_S`, it waits that long for one, then flattens and halts. If no trigger price can be found, or the recorded stops disagree on the price, it flattens and halts.
  This amends the rule in specs 0006 and 0010 that a stop is never cancelled while its position is open: the one exception is a stop this check replaces, after its replacement is accepted.
- **AC-10** (unknown order): an order open on Kraken that is not open in the trader's cache (not ours, or ours but closed locally after a fetch error) is `unknown_order`. The trader cancels it. The account is bot only, so this is always safe. If it filled in the meantime, the resulting position is judged by AC-6 and AC-7.
- **AC-11** (local only order): an order open in the cache that Kraken does not have is `local_only_order`. Before marking it, the check recovers its fills. It fetches Kraken's fill reports for the instrument over `RECONCILE_LOOKBACK_MINS` and matches them to the order by venue order id, falling back to the client order id. A fetch error, or a fetch the build cannot confirm is complete (a full page with no way to page on), ends the check `failed` with `exchange_unknown` (AC-4), never "no fills". When fills match, they go to `exec_engine.reconcile_execution_report` as Kraken returned them (price, venue time, trade id), with an order status report: `FILLED` when they add up to the order quantity, else `CANCELED` with the filled quantity, because Kraken no longer has the order. The fills land on our own order and its position (action `adopted`, `cache_fix: recovered`). A fill whose trade id the cache already holds is skipped, so a repeat run never counts it twice. When none match, the order is marked canceled locally through Nautilus reconciliation (action `marked_canceled`). No fills in the window is not proof of no fills: the position compare after it is the backstop. When any fill was recovered, the check runs the quiet compare and confirming fetch (AC-27) once more before it plans the position and stop actions. Only one recovery pass runs per check; if the second pass still finds orders to recover, the check ends `failed`. Recovery only corrects the books, so it also runs while flattening or halted (AC-28). If it was an entry, the strategy may enter again only after the next clean check.
- **AC-12** (duplicate entry): two or more open entry orders (not reduce only) in the cache for the same strategy and instrument, or a position whose fills come from two or more different entry orders of the same strategy since it was last flat, is `duplicate_entry`. An order on Kraken the cache does not hold is always `unknown_order` (AC-10), never a duplicate. The trader cancels every open entry order, closes the position, and latches a halt with `reconcile_mismatch`.
- **AC-13** (every finding is visible): each finding writes one `reconciliation_mismatches` row with the action taken and its result, one `critical` `reconcile_mismatch` risk event keyed `reconcile:<mismatch id>` (the id is a uuid7 made once per finding, so a retried or spooled write never duplicates the row or the alert), and one CRITICAL log line carrying that `event_key`, which the alerter (spec 0013 AC-3) pushes to your phone within 60 s.

*Duplicates*
- **AC-14** (unresolved order): while any entry order (not reduce only) is submitted without an acknowledgement, or was marked closed by Nautilus's in flight check since the last clean check, entries on that instrument are blocked with the new reason `order_unresolved`. It clears on the order's acknowledgement or the next clean check. The actor tracks these orders in memory (in flight entries from the cache, plus entries the in flight check rejected); after a restart, `reconciling` already blocks entries until the start check.
- **AC-15** (no warm up re entry): after a start or reconnect check, a strategy ignores any entry signal whose triggering bar or quote has a `ts_event` before that check finished clean. A strategy that runs on a timer acts only on ticks scheduled after the check. A restart can never fire the same signal twice.

*Strategy resume*
- **AC-16** (resume position): `KfsStrategy` gains a `resume_position(position, stop_order)` hook. After a clean start check, the owning strategy (the strategy id on the cached position) is called once per known position, after the stop fixes of AC-9, so `stop_order` is always the one stop left. It runs after the start check only; reconnect and periodic checks never call it, because the process kept its memory. It rebuilds its exit timers from the position's open time and keeps the stop, then manages the position as if no restart happened. When the maximum hold time has already passed, it exits at once. `BuyAndHold` keeps holding, and `RandomTaker` uses its normal timed exit. Any resting entry order it finds is cancelled. Every strategy base (`BarRuleStrategy`, `ScalpStrategy`, `PlaceholderTimed`, `MacdBaseline`, `BuyAndHold`, `RandomTaker`) implements it. A position whose strategy is not loaded is flattened as AC-7.

*Report and dashboard*
- **AC-17** (report): every `start` and `reconnect` check, and every `periodic` check whose outcome is `mismatch` or `failed`, writes one `reconciliations` row. A clean periodic check only updates `last_clean_check_ns` in `trader_state`. A check that cannot write Postgres still decides and acts. The row goes through the recording queue and spool (spec 0006 AC-12, AC-13).
- **AC-18** (three state card): the dashboard's position card shows three columns. *Wanted* is the trader's latest intent for that instrument (`hold`, `entry_submitted`, `flatten_submitted`, `stop_replacing`). *Trader* is the Nautilus cache position. *Kraken* is the exchange view from the last check, with its age. When Trader and Kraken differ in side or quantity, the card says so in the alert colour. A missing exchange view, one with `status: unknown` (written on every failed fetch), or one older than 3 × `RECONCILE_INTERVAL_S` shows "Kraken unknown", never "flat".
- **AC-19** (badge and history): the top bar shows a check badge: the last clean check time, or `mismatch`, `halted`, or `Kraken unknown` in the alert colour. A new Reconciliation page lists `reconciliations` rows newest first, 50 per page, each with its mismatches and actions.
- **AC-20** (recheck): a new `recheck` control command, sent from a dashboard button or `control_cli`, runs a check with trigger `periodic` at once and answers `executed` with `{outcome, reconciliation_id}`, or `failed` with the reason. A recheck that arrives while a check runs gets the result of the next check.

*Alerts*
- **AC-21** (stuck blocks alert): `reconciling`, `exchange_unknown`, and `order_unresolved` join the alerting set of the stuck entry block rule (spec 0013 AC-6), with its existing threshold.

*Drill mode and proof*
- **AC-22** (drill only live mode): with `TRADING_ENV=live` and `LIVE_MODE=drill`, the env guard allows the Kraken execution client for the trader. The only strategy loaded is `reconcile_drill`, and `PAPER_STRATEGY` is ignored. `reconcile_drill` sends orders only on a `drill_step` control command from source `cli`. It refuses every other entry, any size above the drill size (the larger of the instrument's `size_increment` and `min_quantity`), and more than one open position. With `LIVE_MODE=off` (the default), live stays refused exactly as today.
- **AC-23** (demo unchanged): demo keeps spec 0006's abandoned close at start. The check still runs in demo, at start and every 60 s, against the sandbox's own orders and positions through the same comparison, so the report, blocks, badge, and card are exercised every day. In demo it only reports: it never cancels, flattens, or halts, because the sandbox view cannot find a real mismatch, only a false one.
- **AC-24** (drill script): `scripts/restart-drill.sh <demo|live> [drill...]` runs the drills in the drill table below. For each it prints the expected outcome, the evidence it collected, and `PASS` or `FAIL`, and exits non zero on any `FAIL`. Demo runs the drills marked demo. Live runs all of them in `LIVE_MODE=drill`.
- **AC-25** (live drill passes): the full live drill passes on the VPS against your real Kraken account before feature 19 starts. Each drill ends flat, with no open order and no halt left behind. Its output is saved in `verify.md`.
- **AC-26** (runbook): `docs/runbooks/restart-recovery.md` explains how to recover from a crash, a network loss, or a Kraken outage with a position open: what each block and finding means, the commands to check Kraken's view, how to reset the cache after a `cache_fix: manual` halt, and when to press Resume. The reset order: let the executor close Kraken's position and confirm Kraken is flat, stop the trader and check that `docker compose ps` shows it stopped, run `python -m trader.cache reset`, start it, and check that the start check is clean. A reset while Kraken still holds a position is safe: the start check finds an `unknown_position`, so it flattens and halts again.

*Races and flattening*
- **AC-27** (quiet compare, confirmed findings): a check compares only when no order of ours is in flight (submitted, pending cancel, or pending update) and no fill arrived since the fetch started. Otherwise it waits briefly and fetches again; after 3 tries it ends `failed` with the reason `not_quiet`. Before acting on any finding, it fetches once more and acts only on findings both fetches agree on. A fill or acknowledgement between the fetch and the cache read can therefore never cause a false halt.
- **AC-28** (during a flatten or halt): while the kill switch is flattening or the trader is halted, a check only reports. The exceptions: it still cancels unknown orders that are not reduce only, it still recovers missed fills (AC-11), and the `ReconcileExecutor` still closes an untrusted instrument (AC-8). It never places or cancels stops, never closes, and keeps the existing halt rather than starting a second one.

## Decision

**Chosen option**: Option 1: NautilusTrader's reconciliation rebuilds the cache, and our own fail closed check (pure comparison in core, a Kraken view behind a port) decides, acts, gates entries, and reports.

Turn Nautilus reconciliation on for live, and add a `trading_core/reconcile/` package. It compares a fail closed exchange view with the Nautilus cache on start, reconnect, a 60 s timer, and on demand. It adopts what our own fills explain, replaces stops safely, cancels unknown orders, flattens and halts on anything else, and persists one report per check. A drill only live mode proves it on the real account before go live.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `htmx` (`.claude/skills/htmx/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

### How a check works

```
trigger (start · reconnect · periodic · recheck)
   │
   ▼
fetch Kraken view ──error──▶ outcome failed · block exchange_unknown · retry with backoff
   │ ok
   ▼
read local view (Nautilus cache, after Nautilus reconciliation)
   │
   ▼
compare()  (pure, core)  ──▶ findings[] + planned actions[]
   │
   ├─ no findings ─▶ clean: clear reconciling / exchange_unknown / order_unresolved
   │
   └─ findings ──▶ run actions (cancel · place stop then cancel old · flatten via kill switch · halt)
                   write report + risk events + CRITICAL logs
                   (halted findings keep entries blocked until your Resume)
```

Actions within one check run in a fixed order: cancel unknown orders, recover the fills of local only orders (AC-11), mark the rest canceled, align the cache with Kraken's position, fix stops, then flatten. When any fill was recovered, the check runs the quiet compare and confirming fetch once more before it plans the position and stop actions, so a recovered stop fill is never treated as a missing stop or a mismatch (AC-11). A halting finding skips the stop fixes and goes straight to the flatten.

The comparison is a pure function in `packages/core/src/trading_core/reconcile/compare.py`. It takes an `ExchangeView` and a `LocalView` (frozen dataclasses of orders and positions, `Decimal` sizes and prices, UTC nanosecond times) and returns findings plus planned actions. It does no I/O. A thin `ReconcileActor` (in `trading_core/node/reconcile.py`) owns the timer, the triggers, fetching, and recording. It runs the order actions through the `ReconcileExecutor` strategy (cancels, stop placement, closes of positions no strategy owns) and halts through the kill switch.

### Where the views come from

| View | Live | Demo |
|---|---|---|
| Exchange | `KrakenExchangeView`: the Kraken Futures HTTP client's `request_order_status_reports(open_only=True)` and `request_position_status_reports`, called directly through a wrapper that raises `ExchangeUnavailable` on any error or timeout (`RECONCILE_FETCH_TIMEOUT_S`) | `SandboxExchangeView`: the sandbox execution client's simulated open orders and positions |
| Local | the Nautilus cache's open orders and positions for this trader, read after Nautilus reconciliation has run | same |

Nautilus settings (live only; demo keeps `reconciliation=False` per spec 0006):

| `LiveExecEngineConfig` field | Value | Why |
|---|---|---|
| `reconciliation` | `True` | rebuild the cache from Kraken at start |
| `reconciliation_lookback_mins` | `RECONCILE_LOOKBACK_MINS` (1440) | enough fill history to explain a position held up to a day |
| `inflight_check_interval_ms` / `threshold_ms` / `retries` | defaults (2000 / 5000 / 5) | resolves submitted orders with no acknowledgement |
| `open_check_interval_secs` / `position_check_interval_secs` | `None` (off) | our own fail closed check is the only periodic check, because the adapter returns empty lists on errors |
| `generate_missing_orders` | `True` (default) | lets Nautilus mark unexplained size as `EXTERNAL`, which AC-6 reads as not ours |
| `TradingNodeConfig.timeout_reconciliation` | `RECONCILE_NAUTILUS_TIMEOUT_S` (30) | bounded start |

Before the node is built, the live trader fetches the Kraken view once through the same wrapper, retrying for up to `RECONCILE_START_TIMEOUT_S`. Nautilus's startup reconciliation therefore never runs against an unreachable Kraken (AC-5).

### Data model sketch

Confirmed in the design conversation. One Alembic migration, `0012`.

**`reconciliations`** (one row per check, per AC-17)

| Field | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | PK | uuid7 |
| `env` | text | no | env check |
| `run_id` | uuid | no | FK `strategy_runs`, env scoped (`_env_fk`) |
| `trigger` | text | no | `start` · `reconnect` · `periodic` |
| `started_at` / `started_ns` | timestamptz / bigint | no | |
| `finished_at` / `finished_ns` | timestamptz / bigint | yes | null while running |
| `outcome` | text | yes | `clean` · `mismatch` · `failed` |
| `exchange_orders` / `exchange_positions` | int | yes | counts Kraken reported; null when `failed` |
| `mismatch_count` | int | no | default 0 |
| `error` | text | yes | why it failed |
| `created_at` | timestamptz | no | |

Index `(env, started_at)`.

**`reconciliation_mismatches`** (one row per finding, N:1 to `reconciliations`)

| Field | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | PK | |
| `env` | text | no | |
| `reconciliation_id` | uuid | no | FK `reconciliations`, env scoped |
| `kind` | text | no | `unknown_order` · `local_only_order` · `unknown_position` · `position_mismatch` · `missing_stop` · `stop_size_mismatch` · `duplicate_entry` |
| `instrument_id` | text | no | FK `instruments` |
| `client_order_id` / `venue_order_id` | text | yes | |
| `order_id` / `position_id` | uuid | yes | FK `orders` / `positions` when known |
| `exchange_side` / `exchange_qty` | text / numeric | yes | Kraken's view |
| `local_side` / `local_qty` | text / numeric | yes | the cache's view |
| `action` | text | no | `cancelled` · `flattened` · `closed_at_market` · `stop_replaced` · `adopted` · `marked_canceled` · `none` |
| `action_result` | text | no | `ok` · `failed` · `pending` |
| `details` | jsonb | no | the raw report fields and the trigger price used |
| `created_at` | timestamptz | no | |

Index `(env, reconciliation_id)`. Rows are kept forever (about one per restart).

Other schema changes in the same migration:
- `control_commands.cmd` check gains `recheck` and `drill_step` (`ControlCmd`).
- None for `strategy_runs`: a drill run is `mode = live` in `env = live`, which the existing checks already allow, with the strategy `reconcile_drill`.

Redis and enum changes (no migration):
- `BlockReason` gains `reconciling`, `exchange_unknown`, and `order_unresolved`, with plain words in `PLAIN_WORDS`.
- `HaltReason` gains `reconcile_mismatch`.
- New Redis set `{env}:reconcile:cache_untrusted` (instrument ids, no TTL), written only by the trader (AC-8).
- `trader_state` gains `last_clean_check_ns`, `last_check_outcome`, and `intent` (a map from instrument to intent).
- New snapshot key `{env}:snapshot:exchange`. The actor writes it after every fetch, using the snapshot envelope with a TTL of 3 × `RECONCILE_INTERVAL_S`. Data: `status` (`ok` or `unknown`), `fetched_ns`, and on `ok` the positions (instrument, side, quantity) and open orders (client and venue order id, instrument, side, type, quantity, trigger price, reduce only) as Kraken reported them. A failed fetch writes `status: unknown` with no positions or orders.

### State transitions

Check outcome per run of the actor:

```
idle ──trigger──▶ fetching ──error──▶ failed (exchange_unknown; retry with backoff) ──▶ fetching
                     │ok
                     ▼
                 comparing ──not quiet 3 times──▶ failed (not_quiet)
                     │
                     ├──no findings──▶ clean (clear reconciling, exchange_unknown, order_unresolved)
                     │findings, confirmed by a second fetch (AC-27)
                     ▼
                 acting ──all ok, nothing halting──▶ mismatch (incl. a crossed stop closed at market;
                     │                                          entries resume on the next clean check)
                     │halting finding (AC-7, AC-8 not explained by recovered fills, AC-12; AC-9 no price, prices disagree,
                     │                 no fresh quote, or replacement stop rejected)
                     ▼
                 halted (latched, spec 0010; only Resume clears it)
```

Checks never overlap. A trigger that arrives while a check runs is queued once.

### Interface surface

| Surface | Kind | Inputs | Outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `recheck` | control stream command | `source` (`dashboard`/`cli`) | `received`, then `executed` with `{outcome, reconciliation_id}` or `failed` with the reason | dashboard session + CSRF, or VPS shell (spec 0004, 0010) | `failed: check_running` is never returned, a running check absorbs it |
| `drill_step` | control stream command | `step` (`rest_order` · `rest_order_and_die` · `open_position` · `flatten`), `source` must be `cli` | `executed` / `failed` with reason | VPS shell only | `failed: not_drill_mode`, `failed: source_not_cli`, `failed: position_open` |
| `python -m trader.drill <foreign-order\|foreign-position\|cancel-stop\|show>` | one off CLI in the trader image (live drill only) | the action | JSON of what Kraken returned | VPS shell, reads the key files | exits 1 unless `LIVE_MODE=drill` |
| `python -m trader.cache reset` | one off CLI in the trader image (live) | none | how many keys it deleted | VPS shell | exits 1 while the trader heartbeat file is fresh (the trader must be stopped); deletes only `trader-<trader id>:*` (the same rule as `purge_demo_cache`) and the `{env}:reconcile:cache_untrusted` set, never our other `{env}:*` keys, so the halt stays latched |
| `GET /reconciliations?page=N` | dashboard page | page | the history list (AC-19) | dashboard session | 404 past the last page |
| position card, top bar badge | dashboard partials over SSE | none | AC-18, AC-19 | dashboard session | stale or missing snapshot shows "Kraken unknown" |
| `scripts/restart-drill.sh <demo\|live> [drill...]` | laptop or VPS script | env, optional drill ids | per drill expected, evidence, PASS/FAIL | shell | exit 1 on any FAIL |

`rest_order_and_die` submits the resting order and calls `os._exit(1)` right after, before any acknowledgement can arrive. `foreign-order` and `foreign-position` send an order through the Kraken Futures HTTP client with a client order id `drill-foreign-<ns>`, which the trader has never seen.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| any check | Kraken orders and positions | `KrakenExchangeView` (live) or `SandboxExchangeView` (demo), fail closed |
| any check | local orders and positions | Nautilus cache, open orders and positions for this trader id |
| known position (AC-6) | which fills are ours | the cache position's fills and their orders: strategy id is a loaded strategy or `ReconcileExecutor`, or a kill switch exit; never `EXTERNAL` or a Nautilus reconciliation generated order |
| order matching (AC-10, AC-11) | same order | `venue_order_id`, falling back to `client_order_id`. Prices are not compared |
| fill recovery (AC-8, AC-11) | missed fills of a local only order | `KrakenFuturesHttpClient.request_fill_reports(account_id, instrument_id, start=now − RECONCILE_LOOKBACK_MINS)` through the raising wrapper, matched by `venue_order_id`, else `client_order_id` (the adapter's own `_match_fill_reports_for_order` rule); filled quantity = the sum of matched fills, `FILLED` when it reaches the order quantity, else `PARTIALLY_FILLED` |
| residual close (AC-8) | quantity and side | Kraken's position from a fresh fetch right before each attempt: its quantity, on the closing side, reduce only market, sent by the `ReconcileExecutor`; nothing when Kraken is flat |
| untrusted cache (AC-8) | which instruments skip the strategy's own close | Redis set `{env}:reconcile:cache_untrusted`, added on a `cache_fix: manual` finding, cleared by `trader.cache reset` or a clean start check |
| wait for flat (AC-8) | is an untrusted instrument flat | a fresh Kraken fetch through the raising wrapper (an error counts as not flat) |
| recovered fill (AC-11) | price, time, trade id | the Kraken `FillReport` as returned; skipped when the cache already holds that trade id |
| stop check (AC-9) | does the position need a stop | `uses_stop` of the strategy whose id is on the cached position; a position no loaded strategy owns has no stop rule, because AC-7 closes it |
| position matching (AC-6 to AC-8) | same quantity | exactly equal after rounding both to the instrument's `size_increment` (`Decimal`) |
| stop re-place (AC-9) | trigger price | first: the reduce only `STOP_MARKET` orders for that position in the Nautilus cache (Redis survives restarts in live), newest by `ts_init`. Else: the newest `orders.trigger_price` with `order_type = stop_market`, `reduce_only`, and `parent_client_order_id` equal to the position's opening order. Else: none, so flatten and halt. Open stops with different trigger prices: flatten and halt. |
| stop re-place (AC-9) | quantity | Kraken position quantity |
| stop re-place (AC-9) | "already crossed" | last quote's mid for that instrument, no older than `RISK_STALE_QUOTE_S` (wait that long for one, else flatten and halt), against the trigger: long crossed when mid ≤ trigger, short when mid ≥ trigger |
| unresolved (AC-14) | in flight entries | in memory: the cache's in flight entry orders plus entries the in flight check rejected since the last clean check |
| resume (AC-16) | exit timers | the cached position's `ts_opened` plus the strategy's own config (max hold and so on) |
| AC-15 | when the check finished | `reconciliations.finished_ns`, held in memory by the actor and passed to strategies; compared with the triggering bar or quote's `ts_event` |
| reconnect trigger (AC-3) | network back | `StaleQuoteBlock` clearing `stale_feed` (the market and private sockets share one network; 1.231.0 exposes no private reconnect signal) |
| card (AC-18) | Wanted | `trader_state.intent` for the instrument, set by `KfsStrategy.submit_order` (entry or exit), the kill switch (flatten), and the actor (stop replacing); back to `hold` on the order's terminal event |
| card (AC-18) | Trader | `{env}:snapshot:position` (spec 0006) |
| card (AC-18) | Kraken, age | `{env}:snapshot:exchange`, age = now minus `fetched_ns`; `status: unknown` shows "Kraken unknown" |
| card (AC-18) | disagree | Trader and Kraken differ in side or quantity |
| badge (AC-19) | last clean time, outcome | `trader_state.last_clean_check_ns`, `last_check_outcome`; `halted` from the halt key |
| history (AC-19) | rows | Postgres `reconciliations` joined to `reconciliation_mismatches` |
| drill `rest_order` | price, size | last quote's mid × (1 − `DRILL_REST_DISTANCE_PCT`) post only buy; drill size = the larger of the instrument's `size_increment` and `min_quantity` |
| drill `open_position` | size, stop | market buy of the drill size; reduce only stop at the fill price from the fill event × (1 − `DRILL_STOP_PCT`) |

### Key invariants

- An error from Kraken never reads as "flat" or "no orders". It is always `failed` and `exchange_unknown`.
- A known position is never left without a stop: the replacement stop is accepted before the old one is cancelled. This is the only exception to the rule in specs 0006 and 0010 that a stop is never cancelled while its position is open.
- A check acts only on findings two fetches agree on, taken while nothing of ours is in flight (AC-27).
- While flattening or halted, a check never touches stops or closes (AC-28).
- In demo, a check never acts (AC-23).
- Every finding that cannot be explained ends in a closed position and a latched halt. Nothing but your Resume re enables entries after one.
- The trader never trades on a node whose Nautilus reconciliation failed. It exits instead.
- At most one check runs at a time.
- `reconcile_drill` can never hold more than the instrument's minimum size, and can only act on a `cli` command.
- A live deploy still waits for flat (spec 0005 unchanged). Adoption is for crashes, not deploys.

### Security model

- `recheck` uses the existing control path: an authenticated dashboard session with a CSRF token, or a VPS shell (spec 0004, 0010). It only reads and reports, so its blast radius is one extra check.
- `drill_step` is accepted only with `LIVE_MODE=drill` and `source=cli`. The dashboard has no button for it and cannot send it.
- `LIVE_MODE` is set only in `/etc/kfs/kfs.env` on the VPS (spec 0005). It defaults to `off`, so a missing line keeps live refused.
- `trader.drill` reads the Kraken key files under `/run/secrets` through `trading_core.secrets` (spec 0003) and refuses to run unless `LIVE_MODE=drill`. It logs no key material.
- The dashboard reads the new tables through its existing read only Postgres role and the new snapshot key through its Redis ACL user, which gains read on `{env}:snapshot:exchange`.

### Configuration required

All have defaults, so no VPS env line is required to deploy.

- `RECONCILE_INTERVAL_S` [60]: above 0. The periodic check.
- `RECONCILE_RETRY_S` [5]: above 0. The first retry after a failed fetch, doubling up to 60.
- `RECONCILE_START_TIMEOUT_S` [300]: above 0. How long the start check retries before exit 1.
- `RECONCILE_FETCH_TIMEOUT_S` [10]: above 0. Per request.
- `RECONCILE_LOOKBACK_MINS` [1440]: above 0. Nautilus fill lookback.
- `RECONCILE_NAUTILUS_TIMEOUT_S` [30]: above 0. `timeout_reconciliation`.
- `LIVE_MODE` [`off`]: `off` or `drill`. Feature 19 adds `trade`.
- `DRILL_REST_DISTANCE_PCT` [20]: 5 to 50. How far below the mark the resting drill order sits.
- `DRILL_STOP_PCT` [2]: 0.5 to 10. The drill position's stop distance.

### Drills

Each drill prints its expected outcome, the evidence it collected (log lines, `reconciliations` rows, the Kraken view), and PASS or FAIL. The live script runs on the VPS itself and reads `docker logs`, Postgres, and `control_cli`. Kill means `kill 1` inside the container (spec 0006 verify.md: `docker kill` counts as a manual stop).

| # | Drill | Env | Expected | Covers |
|---|---|---|---|---|
| D1 | clean restart while flat | demo, live | `start` check `clean`, `reconciling` clears, entries allowed | AC-1, AC-17, AC-23 |
| D2 | resting entry across a kill | live | Nautilus reconciles the order, start check clean, the strategy's resume cancels it, one order ever on Kraken | AC-1, AC-6, AC-16 |
| D3 | lost acknowledgement (`rest_order_and_die`) | live | exactly one order on Kraken after the restart, `order_unresolved` until it resolves, no duplicate | AC-11, AC-14, AC-15 |
| D4 | unknown order (`trader.drill foreign-order`) | live | within 60 s an `unknown_order` finding, cancelled, critical push | AC-2, AC-10, AC-13 |
| D5 | known position across a kill | live | adopted, stop unchanged, clean, `resume_position` called | AC-6, AC-16 |
| D6 | stop removed while down (`trader.drill cancel-stop`, then restart) | live | `missing_stop`, stop re placed at the recorded trigger price before anything else | AC-9, AC-13 |
| D7 | unknown position (`trader.drill foreign-position`) | live | `unknown_position`, flattened, halt `reconcile_mismatch`, Resume needed | AC-7, AC-13 |
| D8 | network cut for 90 s (longer than `RISK_STALE_QUOTE_S`) with a position open | demo, live | `stale_feed`, then `reconciling`; a `reconnect` check clean when back; stop on Kraken untouched | AC-3, AC-4 |
| D9 | Kraken unreachable at start (trader detached from the network) | demo, live | `exchange_unknown`, retries, exit 1 after the start timeout, healthy once reattached | AC-4, AC-5 |
| D10 | halted restart | demo, live | comes back halted, flattens 5 s after start (spec 0010), check still runs | AC-1 |
| D11 | recheck from the dashboard | demo, live | `recheck` answers `executed` with `clean` | AC-20 |
| D12 | stop of the wrong size (`trader.drill cancel-stop`, then a half size reduce only stop placed by hand through `trader.drill`, then restart) | live | `stop_size_mismatch`, the full size stop placed first, then the half size stop cancelled, clean on the next check | AC-9, AC-13 |

### Critical test scenarios

- Pure comparison, one unit test per finding kind and per action, over recorded Kraken report shapes, including the partial fill that our fills explain (adopt) and the one they don't (flatten and halt). Verifies **AC-6** to **AC-12**.
- Fail closed: the wrapper raising on HTTP 503, timeout, and malformed JSON gives `failed` and `exchange_unknown`, never an empty view. Verifies **AC-4**.
- Stop replace order: the new stop is accepted before the old is cancelled. A crash between the two steps leaves two stops, and the next check keeps the target and cancels the rest. A rejected replacement flattens and halts. A crossed trigger closes at market with no halt. No trigger price, disagreeing prices, or no fresh quote flattens and halts. Verifies **AC-9**.
- Races: a fill between fetch and cache read gives no finding. A finding the second fetch does not confirm is not acted on. Three busy tries end `not_quiet`. Verifies **AC-27**.
- Flattening and halted: a check finds a missing stop and does nothing but report. Demo findings are never acted on. Verifies **AC-28**, **AC-23**.
- Start: the heartbeat file stays fresh through a 300 s retry. The watchdog exits 1 when the strategies never start. Verifies **AC-5**.
- The kill switch flatten includes the `ReconcileExecutor`, and an unknown position is closed by it. Verifies **AC-7**.
- Fill recovery: a local only stop with a matching Kraken fill is adopted and closes the strategy's own position, with no `missing_stop` and no halt. A partly filled one ends `CANCELED` with its filled quantity. A repeat run skips fills already held. A local only order with no fill is marked canceled. A fill fetch error ends `failed` with `exchange_unknown`. A second pass that still finds orders to recover ends `failed`. Recovery that leaves the cache below Kraken takes the `synced` path. Verifies **AC-7**, **AC-11**.
- Untrusted cache: a cache larger than Kraken after recovery halts with `cache_fix: manual` and adds the instrument to the untrusted set. The executor closes only Kraken's freshly fetched quantity, reduce only, and sends nothing when Kraken is flat. The kill switch flatten, the re-flatten, and the halted restart flatten skip the strategy's own close for it. The wait for flat reads Kraken for it and does not time out. `trader.cache reset` refuses while the heartbeat is fresh, and clears the set. Verifies **AC-8**, **AC-28**.
- Stopless strategies: a `BuyAndHold` or `RandomTaker` position with no stop gives no `missing_stop`; a position of a strategy that uses a stop still does. Verifies **AC-9**.
- Duplicate: two open entry orders for one strategy give `duplicate_entry`, cancel all, flatten, halt. Verifies **AC-12**.
- `order_unresolved` blocks a second entry while one is unacknowledged. A signal from data before the check's finish time is ignored. Verifies **AC-14**, **AC-15**.
- Report: start and reconnect always write a row. A clean periodic writes none and moves `last_clean_check_ns`. A Postgres outage spools the row. Integration test on real Postgres. Verifies **AC-17**.
- Env guard: live with `LIVE_MODE=off` refuses the Kraken client. With `drill` it allows only `reconcile_drill`. `drill_step` from `dashboard` is refused. Verifies **AC-22**.
- Dashboard: a missing or stale exchange snapshot renders "Kraken unknown". Disagreeing columns render the alert state. Verifies **AC-18**, **AC-19**.
- The demo drill set D1, D8, D9, D10, D11 in Compose on the laptop, then the full live drill on the VPS. Verifies **AC-23** to **AC-25**.

## Build plan

Tracer Bullet: the first slice is one thin real thread (a demo start check → report row → entry block → dashboard badge). Then each segment thickens end to end: live wiring, actions, strategy resume, dashboard, drill mode, and the proof.

1. [x] **Thin thread in demo.** Migration `0012` (both tables, `ControlCmd` check). Pure `trading_core/reconcile/` with `ExchangeView`, `LocalView`, `compare()` returning findings (clean only for now), `SandboxExchangeView`. `ReconcileActor` runs a `start` check before strategies may enter, with the `reconciling` block, a `reconciliations` row through the recording queue, `last_clean_check_ns` and `last_check_outcome` in `trader_state`, and the top bar badge. Satisfies **AC-1**, **AC-17**, **AC-19** (badge), **AC-23**.
2. [x] **Live wiring, fail closed.** Live `LiveExecEngineConfig` per the table. `KrakenExchangeView` with the raising wrapper. The pre build start fetch with retry, the heartbeat touch, and exit 1. The startup watchdog. `exchange_unknown` with backoff. The 60 s timer, the `reconnect` trigger from `stale_feed` clearing, and the `recheck` command (CLI and dashboard button). `{env}:snapshot:exchange`. Satisfies **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-20**.
3. **Findings and actions.** All seven finding kinds in `compare()`, with unit tests over recorded report shapes. The `ReconcileExecutor` strategy, joined to the kill switch flatten. The actions in their fixed order: cancel, place stop then cancel others, crossed trigger closes at market, flatten and halt through `KillSwitch.halt_automatic` with `HaltReason.RECONCILE_MISMATCH`. The quiet compare with a confirming fetch, and report only while flattening, halted, or in demo. Mismatch rows, risk events, CRITICAL logs. Fill recovery for local only orders with the compare again after it, the untrusted cache set with the kill switch skip and the Kraken based wait for flat, the reduce only residual close by the executor, `KfsStrategy.uses_stop`, and the `python -m trader.cache reset` command. Satisfies **AC-6** to **AC-13**, **AC-27**, **AC-28**.
4. **Duplicates and resume.** The `order_unresolved` block from in flight orders. The fresh signal rule in each strategy base. `resume_position` on `KfsStrategy`, implemented by every strategy base, plus flatten for a position with no loaded strategy. Satisfies **AC-14**, **AC-15**, **AC-16**.
5. **Dashboard.** `trader_state.intent`, the three state position card, the Reconciliation history page, and the Redis ACL read on the new key. Satisfies **AC-18**, **AC-19**.
6. **Alerts.** Add the three new reasons to the stuck entry block alerting set. Satisfies **AC-21**.
7. **Drill mode.** `LIVE_MODE` setting, the env guard allowlist change for `("trader", "live")` gated on `drill`, the `reconcile_drill` strategy, `drill_step`, and `python -m trader.drill`. Satisfies **AC-22**.
8. **Drill script and runbook.** `scripts/restart-drill.sh` with D1 to D12, and `docs/runbooks/restart-recovery.md`. Run the demo drill set on the laptop. Satisfies **AC-24**, **AC-26**.
9. **Live proof.** Deploy (always `--no-deps`), set `LIVE_MODE=drill` on the VPS, run the full live drill, save the output to `verify.md`, then set `LIVE_MODE=off` and restore the demo paper loop. Satisfies **AC-25**.

## Migration plan

**Strategy**: feature flagged.
**Phases**:
1. Slices 1 to 6 ship to the VPS demo. Live behaviour is unreachable while `LIVE_MODE=off`, so demo keeps running `donchian_scalp` with the check exercised daily.
2. Slice 7 adds drill mode, still off by default.
3. The live drill switches the VPS to live drill mode for one session (the demo paper loop stops for that time), then back to demo.

**Rollback**: revert the release. Migration `0012` only adds tables and widens a check, so the old code runs on the new schema. Setting `LIVE_MODE=off` ends any drill at once.
**Risks**: the live drill runs on the real account. The caps in AC-22 bound it to one minimum size position (about 10 USD notional at the time of writing), costing cents in fees and spread. A bug in drill mode could place an unwanted order, which the D4 and D7 paths (cancel, flatten, halt) and your phone alerts catch.

## Consequences

**Positive**:
- A crash, restart, or network loss no longer leaves a live position unmanaged or a stop missing. What the trader can explain it keeps trading, and what it can't it closes and tells you about.
- Errors from Kraken can never make the trader believe it is flat. That closes the adapter's fail open gap.
- Every check leaves evidence, and the three state card shows where wanted, believed, and confirmed disagree.
- The restart paths are proven on the real account before any real capital is at risk.

**Negative / tradeoffs**:
- Every unexplained finding halts, so a Kraken glitch or a bug in our comparison can stop trading until you press Resume. This is deliberate.
- About two extra REST calls a minute, and a start that can take up to 5 minutes during a Kraken outage before exiting.
- Every strategy base must implement `resume_position`, and each new strategy must too. A strategy that gets it wrong mismanages an adopted position.
- The live drill pauses the VPS demo paper loop for its session, and it costs a few cents.
- Demo still loses positions on restart (spec 0006). The demo check proves the code path, not Kraken behaviour.
- A lost Redis cache halts even a position our fills would have explained, because only generated fills remain (AC-6). This is deliberate: without the cache, nothing proves the position is ours.
- A cache that holds more than Kraken, and that recovered fills cannot explain, needs a hand reset (AC-8). It should be rare, because the common cause, a missed fill of our own order, recovers by itself.

**Neutral**:
- Migration `0012` (two tables, a widened check).
- Nautilus's own periodic open and position checks stay off. Our check replaces them.
- `LIVE_MODE` becomes the switch feature 19 extends with `trade`.

## Follow-up

- [ ] `/sync`: apps/trader/AGENTS.md says the trader "reloads state and then reconciles with Kraken", but reconciliation is off today (`main.py:195`). Update it once this ships, and add the `LIVE_MODE` and drill conventions.
- [ ] Feature 19: extend `LIVE_MODE` with `trade`, and make a passing live drill (AC-25) a go live precondition.
- [ ] When NautilusTrader is upgraded past 1.231.0, recheck whether the Kraken adapter still turns report errors into empty lists, and whether a private websocket reconnect signal exists (it could replace the `stale_feed` proxy for AC-3).
- [ ] Before the live drill, confirm the PF_XBTUSD minimum size and its notional at that day's price, and that the account's leverage settings (spec 0011) accept it.
