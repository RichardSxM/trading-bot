# 0006. Run one timed placeholder strategy end to end on real prices and the sandbox, recorded and shown live

**Date**: 2026-10-04
**Status**: In Progress

## Summary

This is the walking skeleton: the first time the bot trades by itself. A deliberately simple strategy enters a small BTC perpetual position every 15 minutes on real Kraken prices, protects it with a stop, and closes it after 10 minutes. The fills are simulated by NautilusTrader's sandbox (a fake exchange inside the trader), because Kraken closed its futures demo. A pure risk check guards every entry, every order and fill lands in Postgres, and the dashboard shows the live position and P&L. Proving this thread end to end is the point. The strategy is not meant to make money.

## Requirements

**User stories**:
- As the trader's owner, I want the bot to open and close positions by itself on real prices so I can see the whole loop work before any strategy research.
- As the owner, I want every entry protected by a stop and checked against fixed caps so a bug cannot grow a position.
- As the owner, I want every decision, order, and fill in Postgres so any trade traces back to why it happened.
- As the owner, I want the dashboard to show the live position, P&L, recent trades, and why the bot is not trading when it is not.

**Acceptance criteria**:
- **AC-1** (gate): before any other build work, a throwaway TradingNode with the sandbox execution client on `PF_XBTUSD.KRAKEN` and Kraken's public quotes proves four things:
  - A market entry fills at a price within the bid and ask at submit.
  - A reduce only stop market fills when the quote crosses its trigger.
  - A reduce only market order sent while flat is denied or cancelled, never filled into a new position.
  - The trader's Redis login can `SCAN` and `UNLINK` keys matching `trader-TRADER-DEMO:*`.

  If any of these fails, the build stops and the feature returns to `/architect`, with no workaround.
- **AC-2**: in demo the trader starts with the `placeholder_timed` strategy registered. The env guard allows exactly the `SANDBOX` execution client for `("trader", "demo")`, and still none for live.
- **AC-3**: when flat and no entry block is active, the strategy decides an entry 2 s after each 15 minute UTC boundary (:00, :15, :30, :45).
  - It uses the 1 minute bars whose `ts_event` is at or before the boundary.
  - The side is `buy` when the newest such bar's close is at or above the close `PLACEHOLDER_LOOKBACK_MIN` bars earlier, else `sell`.
  - With fewer than `PLACEHOLDER_LOOKBACK_MIN + 1` such bars, it logs `entry_skipped_warmup` and writes no decision.
  - Each decision is written to the recording queue with its inputs before any order is submitted.
- **AC-4**: the entry quantity is `PLACEHOLDER_NOTIONAL_USD` divided by the current mid price, rounded down to the instrument's size step. The entry is a market order, GTC, tagged `decision:<uuid>`.
- **AC-5**: the pure risk check returns the first matching refusal in this order:
  1. `paused`
  2. `blocked:<reason>` (the first active block, in sorted order)
  3. `position_open`
  4. `zero_quantity`
  5. `notional_cap` (the order's notional is over `RISK_MAX_ORDER_NOTIONAL_USD`)
  6. `leverage_cap` (open notional after the order divided by equity is over `RISK_MAX_LEVERAGE`)

  A refusal writes the decision with `risk_verdict = rejected` and that reason as `risk_reason`, writes a `limit_hit` risk event, and submits nothing. The event is `info` for `paused`, `blocked`, or `position_open`, and `warning` for the others. NautilusTrader's RiskEngine also refuses any order over `RISK_MAX_ORDER_NOTIONAL_USD` notional.
- **AC-6**: when the entry order is completely filled, the strategy submits one reduce only stop market for the position's full quantity. The stop is `PLACEHOLDER_STOP_PCT` away from the position's average entry price (below for long, above for short), rounded away from the market to the tick. It uses trigger type `DEFAULT`, is GTC, and is tagged `decision:<entry decision>` and `parent:<entry client order id>`. If the entry is still only partly filled 5 s after its first fill, the strategy runs the exit loop (AC-8a) instead.
- **AC-7**: if the stop is not `ACCEPTED` within `STOP_ACCEPT_TIMEOUT_S` of the entry's complete fill (because it was rejected, denied, or is still pending), the strategy runs the exit loop. It also blocks entries with `stop_not_accepted` until restart and writes a `critical` `order_rejected` risk event.
- **AC-8**: `PLACEHOLDER_HOLD_MIN` after the entry's first fill, the strategy does three things:
  1. Records an `exit` decision (`reason = time`, `risk_verdict = approved`, `risk_reason` NULL).
  2. Runs the exit loop with that decision.
  3. Leaves the stop resting until the position is closed.
- **AC-8a** (exit loop): the strategy submits a reduce only market order, GTC, for the position's quantity.
  - If it is rejected or denied while the position is still open, the strategy submits a new one every 1 s.
  - After the 5th failure it writes one `critical` `order_rejected` risk event, blocks entries with `exit_failed` until restart, and keeps retrying every 1 s.
  - A rejection or denial that arrives once the position is flat is logged at INFO and ignored.
- **AC-9**: on `PositionClosed` (from the stop, the exit loop, or both), the strategy cancels any resting stop, any pending exit order, and the hold and acceptance timers. It then waits for the next boundary. A stop fill during the exit loop raises no risk event and no block.
- **AC-10**: the sandbox account starts at `SANDBOX_STARTING_BALANCE_USD`, as a margin account with netting and default leverage `RISK_MAX_LEVERAGE`.
  - Each fill charges `SANDBOX_MAKER_FEE` or `SANDBOX_TAKER_FEE` of its notional, as NautilusTrader's commission in USD at USD precision. The commission lands in `fills.fee`.
  - `positions.realized_pnl` is NautilusTrader's realized P&L, which is net of commissions.
  - `run_config` records the balance, the fees, and the fill source.
- **AC-11**: every order event, fill, and position change reaches Postgres through the spec 0002 repository, in `orders`, `order_events`, `fills`, and `positions`.
  - At start: `instruments`, `strategies`, `strategy_versions`, and a `paper` `strategy_runs` row.
  - Every 60 s and on every fill: `account_snapshots`, plus `position_snapshots` while a position is open.
  - Each `OrderFilled` the recording actor receives produces exactly one INFO log line `fill_seen` with its `trade_id`.
  - Replaying the same events writes nothing new.
- **AC-12**: Postgres writes run on a writer thread fed by a bounded queue, never on the event loop.
  - **Transient errors** (connection and timeout errors) are retried in order, after 1, 2, then 5 s and every 5 s after that. Entries are blocked with `recording_unhealthy` while the oldest queued item is older than `RECORDING_MAX_BACKLOG_S`, or while the newest write attempt failed. The block clears once the queue is empty and the last write succeeded.
  - **A `RecordConflictError`** calls `report_conflict`, sends the item to the dead letter file, and blocks entries with `record_conflict` until restart.
  - **Any other error** (`MissingParentError`, an integrity or data error, a mapper exception) sends the item to the dead letter file, logs ERROR `recording_failed`, and blocks entries with `recording_failed` until restart.
  - A full queue counts as a failed write. The item goes to an in memory overflow list that is spilled on stop.
  - Exits and stops are never blocked.
- **AC-13**: after the node has stopped (strategies stopped, `timeout_post_stop` over), `run_node` drains the queue for up to `RECORDING_DRAIN_S`. Anything left goes to `SPOOL_DIR/<run_id>.jsonl`.
  - The next start replays every spool file through the repository before trading, then deletes it. A replayed item that conflicts goes to the dead letter file and blocks entries with `record_conflict`. A truncated last line is skipped with a WARNING.
  - Dead letter files (`SPOOL_DIR/deadletter-<run_id>.jsonl`) are never replayed automatically.
- **AC-14**: a 1 s timer blocks entries with `stale_feed` while the last quote's `ts_event` is more than `RISK_STALE_QUOTE_S` old. When the block starts, it writes a `stale_feed` warning risk event. The same timer clears the block when quotes are fresh again.
- **AC-15** (demo start): the start sequence runs in this order.
  1. Before the node is built, in demo only, the trader deletes every Redis key matching `trader-<trader_id>:*` with `SCAN` and `UNLINK`. Live never deletes cache keys. NautilusTrader's `flush_on_start` stays `False` in every env.
  2. Replay the spool (AC-13).
  3. `start_run`.
  4. `close_abandoned`: every position still `open` for this env and `trader_id` in any run other than the new one, whatever that run's status, is closed as abandoned:
     - `status = closed`, `side = flat`, `quantity = 0`, and `closed_ns` and `last_event_ns` set to now
     - `avg_exit_px` is the newest position snapshot's `mark_px`
     - `realized_pnl` is the stored `realized_pnl` plus that snapshot's `unrealized_pnl`
     - `fees_total` is unchanged
     - with no snapshot, `avg_exit_px` stays NULL and `realized_pnl` is unchanged

     Every order those runs left open gets `status = canceled` and `last_event_ns = now`. Each abandoned position gets one `critical` `reconcile_mismatch` risk event with key `abandoned:<position_id>`.
  5. Trading starts without a manual step.

  If Postgres is unreachable at start, the trader exits 1 and Docker restarts it.
- **AC-16**: every second the trader publishes `{env}:snapshot:position` and `{env}:snapshot:account` through the snapshot envelope (30 s TTL). The `trader_state` snapshot gains two fields:
  - `entries_blocked`: the sorted active block reasons, plus `paused` when paused.
  - `recording_backlog`: the number of queued and overflow items.
  - `exiting`: true while the exit loop runs, and `position_opened_ns` (null when flat). The spec 0005 soak uses both for its stop carve out.
- **AC-17**: the dashboard Overview shows a position card fed over SSE.
  - While a position is open: side, size, average entry, mark, stop price, and unrealized P&L.
  - Always: realized P&L today (UTC), realized P&L all time, and fees today, each over `paper` runs in this env only.
  - Every P&L figure is labeled "before funding".
  - A missing position snapshot, or one older than the heartbeat's staleness limit (spec 0004), shows "trader offline", never "flat".
- **AC-18**: the dashboard shows a top bar badge whenever `entries_blocked` is not empty, and Overview lists the reasons in the plain words of the *Block reasons* table.
- **AC-19**: the Trades page has two lists, each newest first and 50 per page.
  - **Closed round trips** (`page`): opened and closed times, side, size, average entry and exit, net realized P&L, fees, and exit reason (`stop`, `time`, `stop_failure`, or `abandoned`). They come from `paper` runs only, ordered by `closed_at` then `id`, descending.
  - The side is the opening side (`long` or `short`), taken from the position's first fill, because `positions.side` is `flat` once closed.
  - A round trip with no linked fill is still listed, with side and exit reason blank (unless it is `abandoned`, which comes from `risk_events`).
  - **Latest fills** (`fills_page`), ordered by `filled_at` then `id`, descending.
  - A page past the end shows an empty list with a link back to page 1. A page below 1 or not an integer returns 422.
- **AC-20**: Pause from the dashboard stops new entries.
  - The strategy checks the gate right before submitting. An entry the gate denies anyway, or one NautilusTrader's RiskEngine denies (`OrderDenied`), returns the strategy to flat with an `info` `limit_hit` risk event keyed `entry_denied:<decision_id>`.
  - Open positions keep their stop and still exit on time.
  - Unpause resumes at the next boundary.
- **AC-21** (the scope's Done when): on the laptop in demo, left alone for 2 hours, the bot opens and closes at least 6 positions by itself.
  - Every stop is `ACCEPTED` within 5 s of its entry's last fill, checked by the query in `verify.md`.
  - The `fills` rows in the window equal the `fill_seen` log lines in the window.
  - Fees are non zero.
  - Once the trader is flat and `recording_backlog` is 0, the dashboard's realized figures equal the Postgres sums to the cent.

## Decision

**Chosen option**: Option 1: a thin timed strategy on `KfsStrategy`, a pure core risk check, the sandbox execution client on real Kraken quotes, an off loop recording actor on the spec 0002 repository, and Redis snapshots for the live dashboard.

The placeholder trades on a fixed 15 minute beat and places its stop once the entry is filled. A pure risk check in `packages/core` guards every entry, every exit goes through one retrying exit loop, and a queued writer records everything without ever stalling the event loop.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `fastapi` (`terminalskills/skills`, `.claude/skills/fastapi/`) · `htmx` (`damusix/skills`, `.claude/skills/htmx/`)

## Feature design

**Code layout** (Clean Architecture: pure logic in core, thin Nautilus classes at the edge):

| Path | Holds | Nautilus import |
|---|---|---|
| `packages/core/src/trading_core/risk/check.py` | `RiskLimits`, `EntryRequest`, `RiskVerdict`, `check_entry(request, limits) -> RiskVerdict` (pure, `Decimal` only) | no |
| `packages/core/src/trading_core/risk/sizing.py` | `entry_quantity(notional, mid, size_step) -> Decimal` (round down; zero when below one step) | no |
| `packages/core/src/trading_core/strategy/placeholder.py` | pure rules: `next_boundary_ns`, `entry_side(closes, lookback)`, `stop_price(avg_px, side, pct, tick)`, `exit_reason(...)` | no |
| `packages/core/src/trading_core/strategy/blocks.py` | `EntryBlocks`: a thread safe set of reasons with `add`, `clear`, `active`, like `PauseGate`; the *Block reasons* table is its enum | no |
| `packages/core/src/trading_core/recording/mappers.py` | plain fields (ids, `Decimal`s, ns ints, tags) to `rows.py` rows, testable without Nautilus | no |
| `packages/core/src/trading_core/recording/writer.py` | `RecordingWriter`: bounded `queue.Queue`, worker thread, error classes per AC-12, health (`oldest_age_s`, `last_ok`, `backlog`), `drain(timeout)`, `spill(path)`, `replay(dir)` | no |
| `packages/core/src/trading_core/recording/spool.py` | JSON lines codec for every row dataclass and repository call: `{"call": <function name>, "row_type": <class name>, "row": {...}}`, with `Decimal` and `UUID` as strings, `StrEnum` as values, and ints as ints; it round trips every row type | no |
| `packages/core/src/trading_core/recording/repository.py` | adds `close_abandoned(session, env, trader_id, current_run_id, now_ns) -> list[UUID]` (AC-15) | no |
| `packages/core/src/trading_core/node/placeholder.py` | `PlaceholderTimed(KfsStrategy)` and its `StrategyConfig`: timers, bar and quote subscriptions, order calls, exit loop | yes |
| `packages/core/src/trading_core/node/recording.py` | `RecordingActor(Actor)`: subscribes to `events.order.*`, `events.position.*`, `events.account.*`, maps, enqueues; 60 s snapshot timer | yes |
| `packages/core/src/trading_core/node/sandbox.py` | `sandbox_exec_client(settings) -> SandboxExecutionClientConfig` | yes |
| `apps/trader/src/trader/main.py` | demo cache key purge; registers the sandbox client (demo), the strategy, and the recording actor; the start sequence (AC-15); the drain after stop (AC-13); the two new snapshots | yes |
| `apps/dashboard/...` | `live/poller.py` new partials; `web/page_routes.py` Trades queries; templates `partials/overview_position.html`, `partials/topbar_blocked.html`, `pages/trades.html` | never |

**Recording mapper rules**:
- `OrderRow` is built from the order in the cache after each order event, using `order_type`, `side`, `quantity`, `price`, `trigger_price`, `reduce_only`, `post_only`, `time_in_force`, `status`, `filled_qty`, `avg_px`, and `venue_order_id`, with `last_event_ns` = the event's `ts_event`. The event itself becomes the `OrderEventRow`.
- `decision_id` is parsed from the tag `decision:<uuid>`, and `parent_client_order_id` from `parent:<coid>`. A strategy order without a decision tag is a bug: the mapper raises, so AC-12's `recording_failed` applies.
- The actor keeps a map from `(position_ref, opened_ns)` to the position row's `uuid7`. A NETTING position reopened under the same `position_ref` gets a new `opened_ns`, and so a new row.
- A fill is enqueued on `OrderFilled` with `position_id` NULL. When the position event for that fill arrives, the fill is enqueued again with `position_id` set (spec 0002 allows NULL to value once).

**Strategy timeline** (one round trip):
1. Boundary + 2 s: if a position or an entry order is open, skip without a decision. With too few bars, skip (AC-3). Otherwise build the `EntryRequest` and call `check_entry`. Record the decision (approved or rejected). If approved, check the gate again and submit the market entry.
2. Entry complete: submit the stop and start the acceptance timer (AC-6, AC-7). First fill: start the hold timer (AC-8), and the 5 s partial fill timer.
3. The hold timer or a failed stop starts the exit loop (AC-8a). A stop fill closes the position.
4. `PositionClosed`: cancel the leftover stop, exit orders, and timers (AC-9).

**Block reasons** (what the dashboard shows; the first three clear by themselves, the rest only on restart):

| Reason | Plain words |
|---|---|
| `paused` | Paused from the dashboard or by a deploy |
| `stale_feed` | No fresh prices from Kraken |
| `recording_unhealthy` | The database is behind or not answering |
| `stop_not_accepted` | A stop was not accepted, so the position was closed (restart to clear) |
| `exit_failed` | Closing a position failed 5 times (restart to clear) |
| `record_conflict` | A record contradicted the database (restart to clear) |
| `recording_failed` | A record could not be written (restart to clear) |

**Data model sketch**: no migration, no new table or column. Feature 6 writes only through the spec 0002 repository:

| Table | Written when | Notes |
|---|---|---|
| `instruments` | start | `upsert_instrument` from the adapter's `PF_XBTUSD.KRAKEN` |
| `strategies`, `strategy_versions` | start | name `placeholder_timed`, kind `scalping`; config = the `PLACEHOLDER_*` and `RISK_*` settings, Decimals as strings |
| `strategy_runs` | start, after stop | mode `paper`, `trader_id` from the node, `run_config` = balance, fees, `oms_type`, `account_type`, leverage, fill source `quotes`. `finish_run(completed)` after a stop that leaves no position open, else `aborted` |
| `decisions` | every entry attempt that passes warmup, every time exit | `inputs` = `{close_now, close_lookback, mid, equity, open_notional, quote_age_ms}`; `reason` `timed_entry` or `time` |
| `orders`, `order_events` | every order event | `origin = strategy`; the stop and the exit loop's orders after a stop failure carry the entry's decision, the time exit's orders carry the exit decision |
| `fills`, `positions` | every fill and position event | see the mapper rules |
| `position_snapshots`, `account_snapshots` | every 60 s and on every fill | position snapshot only while open; `granularity = raw`; `liquidation_px` NULL |
| `risk_events` | per *Failure handling* | existing kinds only |

New Redis keys (the trader writes them with `EX 30` through `trading_core.snapshot`; the dashboard reads them under its existing `*:snapshot:*` grant):
- `{env}:snapshot:position`: `{instrument_id, side (long|short|flat), quantity, avg_entry_px, mark_px, unrealized_pnl, stop_px, stop_status, opened_ns, exit_due_ns, currency}`. When flat, only `instrument_id`, `side`, and `mark_px`.
- `{env}:snapshot:account`: `{balance_total, balance_free, margin_used, equity, currency}`.
- `{env}:snapshot:trader_state` gains `entries_blocked`, `recording_backlog`, `exiting`, and `position_opened_ns`. `protective_stops` already exists (spec 0005).

**State transitions**:
- Strategy round trip:
  - `flat → entering` (entry submitted) `→ protected` (stop accepted) `→ exiting` (exit loop running) `→ flat`.
  - From `entering` or `protected`, a stop fill goes to `flat`.
  - From `entering`, a stop not accepted in time, or a partial entry after 5 s, goes to `exiting`.
  - From `entering`, an entry that is rejected, denied, or gate denied goes back to `flat`.
- Entry blocks: per the *Block reasons* table.
- Runs and positions follow spec 0002, plus the abandoned close in AC-15.

**API surface** (no HTTP endpoint changes beyond pages and SSE events):

| Surface | Kind | Inputs | Outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `check_entry(request, limits)` | pure function | `EntryRequest(quantity, mid, equity, open_notional, has_position, paused, blocks)` | `RiskVerdict(approved: bool, reason: str \| None)`, reasons in AC-5 order | n/a | `ValueError` on a negative or zero mid or equity |
| `close_abandoned(session, env, trader_id, current_run_id, now_ns)` | repository | as named | closed position ids | trader_app | `EnvMismatchError` |
| `RecordingWriter.put(item)` | core | a row and its repository call | none | n/a | none raised; failures become blocks (AC-12) |
| `GET /` | page | session | Overview with the position card | logged in session (spec 0004) | 303 to `/login` |
| `GET /trades?page=<n>&fills_page=<m>` | page | both ints ≥ 1, default 1 | round trips and fills | logged in session | 422 on a bad param |
| `GET /events` SSE | events | session | adds `overview-position` and `topbar-blocked` | logged in session | the stream ends on logout |

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| entry decision | boundary time | `next_boundary_ns(now_ns, PLACEHOLDER_INTERVAL_MIN)` on the Nautilus clock (UTC ns), fired at +2 s |
| entry decision | side | bars `PF_XBTUSD.KRAKEN-1-MINUTE-MID-INTERNAL` aggregated from quotes, per AC-3 |
| entry | mid | the latest `QuoteTick` in the cache, `(bid + ask) / 2` |
| entry | quantity | `entry_quantity(PLACEHOLDER_NOTIONAL_USD, mid, instrument.size_increment)` |
| risk check | equity | the sandbox account's `balance_total(USD)`, plus the open position's unrealized P&L at the mid (zero when flat) |
| risk check | open notional after the order | `abs(position qty) × mid + quantity × mid` |
| risk check | caps | `RISK_MAX_ORDER_NOTIONAL_USD`, `RISK_MAX_LEVERAGE` |
| risk check | paused, blocks | `PauseGate.paused`, `EntryBlocks.active()` |
| stale check | quote age | `clock.timestamp_ns()` minus the last quote's `ts_event`, every 1 s |
| stop | trigger price | `stop_price(position.avg_px_open, side, PLACEHOLDER_STOP_PCT, instrument.price_increment)` |
| stop | quantity | `position.quantity` at the entry's complete fill |
| time exit | due time | the entry's first fill `ts_event` + `PLACEHOLDER_HOLD_MIN` |
| exit loop | quantity | `position.quantity` at each submit |
| fill | fee | the sandbox commission from `SANDBOX_MAKER_FEE` / `SANDBOX_TAKER_FEE` (set on the sandbox config, or on the instrument before start; the AC-1 spike records which works) |
| decision, orders | `decision_id` | `uuid.uuid7()` made by the strategy, carried in the order tag `decision:<uuid>` |
| stop | `parent_client_order_id` | the order tag `parent:<coid>` |
| `strategy_versions` | config | the `PLACEHOLDER_*` and `RISK_*` settings as a dict |
| `strategy_runs` | `trader_id` | `node.trader_id` (`TRADER-DEMO`, or the `TRADER_ID` override) |
| demo key purge | key pattern | `trader-<trader_id>:*`, the prefix `use_trader_prefix=True` gives |
| abandoned close | exit price, realized P&L | the newest `position_snapshots` row (`mark_px`, `unrealized_pnl`) plus `positions.realized_pnl`; without a snapshot, see AC-15 |
| position snapshot | mark | the latest quote mid |
| dashboard card | live values | `{env}:snapshot:position` and `{env}:snapshot:account`; offline per AC-17 |
| dashboard | realized today | `SUM(positions.realized_pnl)` joined to `strategy_runs` with `mode = 'paper'`, `status = closed`, `closed_at >= date_trunc('day', now() AT TIME ZONE 'UTC')`, env = `Settings.trading_env` |
| dashboard | realized all time | the same without the date filter |
| dashboard | fees today | `SUM(fills.fee)` joined to `paper` runs, where `filled_at` is today UTC |
| Trades | side | the position's earliest fill (`fills.position_id = positions.id`, ordered by `filled_ns` then `id`): `buy` → `long`, `sell` → `short`. Not `positions.side`, which is `flat` once closed. No linked fill → blank |
| Trades | exit reason | first: a `risk_events` row with key `abandoned:<position_id>` → `abandoned`. Otherwise, from the order of the position's last fill: `stop_market` → `stop`; `market` carrying an `exit` decision → `time`; `market` carrying the entry decision → `stop_failure`. No linked fill and not abandoned → blank |
| Trades | size, fees | `positions.peak_qty`, `positions.fees_total` |
| P&L label | "before funding" | static: feature 6 records no funding (`funding_total` stays 0) |
| AC-21 | fill count | `fill_seen` lines in `docker compose logs trader` over the window, against `fills` rows with `filled_at` in the window |

**Failure handling**:

| Case | Behaviour | Risk event (kind, severity) | event_key |
|---|---|---|---|
| risk check refuses | no order; the decision is rejected | `limit_hit`, per AC-5 | `limit_hit:<decision_id>` |
| entry denied by the gate or the RiskEngine | back to flat | `limit_hit`, `info` | `entry_denied:<decision_id>` |
| entry rejected by the sandbox | back to flat; the next boundary tries again | `order_rejected`, `warning` | `order_rejected:<client_order_id>` |
| stale quotes | block `stale_feed`; clears on fresh quotes | `stale_feed`, `warning` | `stale_feed:<run_id>:<start_ns>` |
| stop not accepted in time | exit loop, block `stop_not_accepted` | `order_rejected`, `critical` | `stop_not_accepted:<client_order_id>` |
| entry partly filled after 5 s | exit loop | `order_rejected`, `warning` | `partial_entry:<client_order_id>` |
| exit loop fails 5 times | block `exit_failed`; keep retrying every 1 s; the stop stays | `order_rejected`, `critical` | `exit_failed:<position_id>` |
| exit order refused while flat | INFO log only | none | n/a |
| transient Postgres error or backlog | block `recording_unhealthy`; retry in order | none (Postgres is the problem); ERROR `recording_unhealthy` once per episode | n/a |
| `RecordConflictError` | `report_conflict`, dead letter, block `record_conflict` | `record_conflict`, `critical` (by `report_conflict`) | spec 0002 |
| other write or mapper error | dead letter, block `recording_failed` | none (it may be the database); ERROR `recording_failed` | n/a |
| queue full (`RECORDING_QUEUE_MAX`) | counts as a failed write; the item goes to the overflow list | none | n/a |
| Redis down | snapshots skipped with one warning a minute (existing behaviour); trading continues | none | n/a |
| Postgres down at start | exit 1; Docker restarts with backoff | none | n/a |
| restart with a position open | AC-15 | `reconcile_mismatch`, `critical` | `abandoned:<position_id>` |
| SIGTERM with a position open | spec 0005: entries cancelled, position and stop kept; the run ends `aborted`; the next start closes the position as abandoned (only a deploy avoids this, by waiting for flat) | as above | as above |

**Key invariants**:
- Every non reduce only order has an approved `decisions` row queued before it is submitted.
- Every exit and stop is reduce only. While a position is open, an accepted stop covers its full quantity, except during the acceptance window after the entry fill, or while the exit loop runs.
- A stop is never cancelled while its position is open.
- At most one position and one entry order exist at a time.
- No money or size as `float`: only `Decimal`, `Price`, `Quantity`, `Money`.
- The event loop never waits on Postgres. Only the writer thread, and the start sequence before the node runs, hold a database session.
- The sandbox is the only execution client in demo. Live still has none until feature 19.
- No process ever flushes a whole Redis database. The demo purge touches only `trader-<trader_id>:*`.

**Security model**: no new secret and no Kraken key. Demo sends nothing to Kraken, and market data stays on the public feed.
- The trader writes as `trader_app`, whose existing INSERT and UPDATE grants cover every table touched (spec 0002, migration `0002`).
- The trader's Redis user already has every command except admin and flush commands, so `SCAN` and `UNLINK` need no ACL change.
- The dashboard reads as `dashboard_app` (SELECT) and reads Redis under its snapshot grant. It stays read only.
- The env guard allowlist gains `SANDBOX` for `("trader", "demo")` only, so a live trader with a sandbox client is refused at start.
- The spool volume holds business records only (no secrets) and is mounted only into the trader.

**Configuration required** (non secret, in `trading_core.settings`, validated at start so a bad value refuses to start; defaults in brackets; all `Decimal` except where noted):
- `PLACEHOLDER_INTERVAL_MIN` [15, int]: must divide 60
- `PLACEHOLDER_HOLD_MIN` [10, int]: at least 1 and below the interval
- `PLACEHOLDER_LOOKBACK_MIN` [5, int]: at least 1
- `PLACEHOLDER_STOP_PCT` [0.3]: above 0 and below 100
- `PLACEHOLDER_NOTIONAL_USD` [100]: above 0. A WARNING at start when it is above `RISK_MAX_ORDER_NOTIONAL_USD`; not refused, because the AC-5 drill relies on it.
- `RISK_MAX_ORDER_NOTIONAL_USD` [250]: above 0. Also the RiskEngine's `max_notional_per_order` for `PF_XBTUSD.KRAKEN`.
- `RISK_MAX_LEVERAGE` [2]: above 0. Also the sandbox's default leverage.
- `RISK_STALE_QUOTE_S` [10]: above 0
- `STOP_ACCEPT_TIMEOUT_S` [5]: above 0
- `SANDBOX_STARTING_BALANCE_USD` [1000]: above 0
- `SANDBOX_MAKER_FEE` [0.0002] and `SANDBOX_TAKER_FEE` [0.0005]: fractions of notional, at least 0 and below 1. These are Kraken's base tier as of 2026; check Kraken's fee page before relying on them.
- `SANDBOX_REJECT_STOP_ORDERS` [false, bool]: for the AC-7 drill only. A start refuses it outside demo and logs a WARNING while it is true.
- `RECORDING_MAX_BACKLOG_S` [30], `RECORDING_QUEUE_MAX` [10000, int], `RECORDING_DRAIN_S` [5]: all above 0. `POST_STOP_S` (10) plus `RECORDING_DRAIN_S` must be at most 25, inside the 30 s Docker stop grace.
- `SPOOL_DIR` [`/data/spool`, path]
- Compose: a named volume `trader-spool` mounted at `/data/spool` on `trader` only (the laptop and the VPS alike).

**Critical test scenarios**:
- Happy path: on the laptop in demo, two hours unattended produce at least 6 round trips, each with an accepted stop and matching fill counts, and a dashboard that matches Postgres. Verifies **AC-3**, **AC-4**, **AC-6**, **AC-8**, **AC-10**, **AC-11**, **AC-17**, **AC-21**.
- Risk refusal: `check_entry` unit tests for each reason and their order, plus a strategy test with `PLACEHOLDER_NOTIONAL_USD=300` that records a rejected decision and submits nothing. Verifies **AC-5**.
- Stop failure: with `SANDBOX_REJECT_STOP_ORDERS=true`, the strategy runs the exit loop within 5 s and blocks entries. Verifies **AC-7**, **AC-8a**.
- Exit race: a strategy test where the stop fills while the time exit is in flight produces no risk event and no block. Verifies **AC-8a**, **AC-9**.
- Postgres outage: `docker compose stop postgres` mid round trip blocks entries but the time exit still runs. When Postgres is back, the queue drains and the block clears, with no missing fill. Verifies **AC-12**.
- Poison item: a writer unit test with a repository stub that raises `MissingParentError` dead letters the item, blocks `recording_failed`, and keeps writing the rest. Verifies **AC-12**.
- Spill and replay: stop the trader while Postgres is stopped. The spool file exists, and the next start replays it with no duplicates. A spool codec test round trips every row type. Verifies **AC-13**.
- Crash while positioned: `docker kill` the trader during a round trip. The next start closes the leftover as abandoned with a critical event, and trades again. A `close_abandoned` integration test covers a `completed` run, an `aborted` run, and a position with no snapshot. Verifies **AC-15**.
- Key purge: an integration test against the Compose Redis shows that the demo purge deletes `trader-TRADER-DEMO:*` and leaves `demo:trader:pause`, `demo:control`, and the snapshots alone. Verifies **AC-15**.
- Stale feed: cut the trader's network for 20 s; entries block and recover. Verifies **AC-14**, **AC-18**.
- Pause: Pause during an open position. The stop and the time exit still work, and no new entry follows. Verifies **AC-20**.
- Trades side: an integration test on Postgres seeds a long round trip, a short one, and a closed position with no linked fill, and gets side `long`, `short`, and blank. Verifies **AC-19**.
- Auth: an unauthenticated `GET /trades` redirects to `/login`, and the dashboard cannot write any table (the existing role tests). Verifies **AC-19**.

## Build plan

Tracer Bullet: the gate first, then the thinnest real thread from quote to dashboard, then thicken each segment. No migration in any slice.

**Milestone 1: gate**
1. [x] Spike (a throwaway script or test, not merged as product code) proving the four AC-1 checks. Record in `verify.md` how fees are applied. Fail means stop and return to `/architect`. Satisfies **AC-1**.

**Milestone 2: thin thread (entry to dashboard)**
2. [x] Settings and their validation; `sandbox_exec_client`; the env guard allowlist entry `SANDBOX` for demo; the demo key purge; the `trader-spool` volume in `compose.yaml`. Satisfies **AC-2**, **AC-10**, **AC-15** (purge part).
3. [x] Pure core: `risk/check.py`, `risk/sizing.py`, `strategy/placeholder.py`, `strategy/blocks.py`, with unit tests. Satisfies **AC-4**, **AC-5**.
4. [x] `PlaceholderTimed(KfsStrategy)`: boundary timer, bars, decision, entry, stop on complete fill, hold timer, exit loop, stop fill and `PositionClosed` cleanup, gate check before submit. Satisfies **AC-3**, **AC-4**, **AC-6**, **AC-8**, **AC-8a**, **AC-9**, **AC-20**.
5. [x] `RecordingActor`, `RecordingWriter`, and the mappers; the start writes (instrument, strategy version, `start_run`) and `finish_run` after stop. Satisfies **AC-11**.
6. [x] The trader publishes the `position` and `account` snapshots; the dashboard Overview gets its position card, P&L figures, and offline state. Satisfies **AC-16**, **AC-17**.

**Milestone 3: failure paths**
7. [x] Stop acceptance and partial entry timers, the `stale_feed` timer, and `entries_blocked` and `recording_backlog` in `trader_state`. Satisfies **AC-7**, **AC-14**, **AC-16**.
8. [x] Writer error classes, dead letter, the spool codec, drain and spill after stop, and replay at start. Satisfies **AC-12**, **AC-13**.
9. [x] `close_abandoned` (integration tests on Postgres) and the full start sequence. Satisfies **AC-15**.
10. [x] The dashboard blocked badge and reasons. Satisfies **AC-18**.

**Milestone 4: trades page and the run**
11. [x] The Trades page: round trips with side from the earliest fill and exit reason, and fills, each paginated. Satisfies **AC-19**.
12. [x] The 2 hour laptop run and the drills in `verify.md`. Satisfies **AC-21** (and rechecks all of the above).

## Consequences

**Positive**:
- The whole thread from Kraken quotes to a dashboard P&L exists and is tested, so features 7 (soak), 9, 11, and 13 build on real behaviour, not guesses.
- The risk check, sizing, and stop rules are pure functions in core that the backtest (feature 13) and live (feature 19) reuse unchanged.
- The trader never stalls on Postgres. A database outage costs entries, never exits, and a failed exit keeps retrying instead of giving up.
- The fixed 15 minute beat makes the soak, the deploy during a position, and the drills easy to schedule.

**Negative / tradeoffs**:
- Sandbox fills ignore queue position and latency and fill a 100 USD market order at the touch, so paper P&L flatters reality. Spec 0001 already says the promotion gate must not trust it alone.
- The sandbox only acts on quotes. With a dead feed, the stop cannot trigger and a time exit fills at the last stale quote. This is a demo only limitation.
- A crash, a SIGTERM, or an autoheal restart while positioned loses the simulated position. AC-15 records it as abandoned at a mark up to 60 s old, so that P&L is approximate. Each one is a critical event, which the soak counts. Only a deploy (which waits for flat) avoids it.
- Demo deletes its own Nautilus cache keys at every start, unlike live. Root and trader `AGENTS.md` must say so, or someone will copy it to live.
- Entering on every beat means about 96 round trips a day of pure fee loss: about 0.1 USD each at 0.05 % taker twice on 100 USD. That's intended, but the demo P&L line slopes down.
- P&L excludes funding. Positions last 10 minutes, so the error is small, but it is not zero across a funding time.
- A partly filled entry is closed after 5 s rather than protected by a resized stop. That's fine for the sandbox, which fills 100 USD at once, but feature 19 may need resizing on Kraken.
- Strategy and risk numbers live in settings, so every settings change makes a new `strategy_versions` row. That is correct, but it fragments the record when you tune often.
- Kraken's real stop behaviour (exchange side triggers, trigger types, rejections) stays unproven until the live canary (spec 0001 Follow-up), because the sandbox is not Kraken.

**Neutral**:
- About 1,440 account snapshot rows a day plus position snapshots, thinned after 90 days by spec 0002's downsampling.
- A new named volume (`trader-spool`) on both the laptop and the VPS. The backup does not need it: a spool exists only between a stop and the next start, and dead letter files are for you to inspect.

## Follow-up

- [ ] `/scope`: feature 6's Done when line says "every entry has an exchange side stop" and "on the demo environment". With the Kraken demo gone, that means a stop resting in the sandbox; align the wording with AC-21.
- [ ] `/sync`: root `AGENTS.md` and `apps/trader/AGENTS.md` should note that the demo trader deletes its own `trader-<trader_id>:*` cache keys at start (sandbox state cannot be restored) while live never does, and that `flush_on_start` stays off everywhere.
- [ ] Spec 0005 AC-24 now allows a position without an accepted stop for up to `STOP_ACCEPT_TIMEOUT_S` after its entry fill, and while the exit loop runs (amended in this change). `kfs-soak` must apply that carve out, and count `fill_seen` lines rather than `OrderFilled`.
- [ ] Spec 0001's dead man's switch gate moves to feature 19: demo sends no order to Kraken, so it only matters live.
- [ ] Feature 19: choose the live stop trigger type (mark or last price), and decide whether partial entries need a resized stop on Kraken.
- [ ] Funding: feature 10 (its Done when already puts funding in P&L) decides how funding reaches `funding_payments` and `positions.realized_pnl` in demo; feature 19 records it from Kraken's account log in live (spec 0002's open Follow-up). Both are on the scope as carried over notes.
- [ ] Spec 0005 milestone 5 (the soak) can start once AC-21 passes; its deploy during a position uses this strategy's 10 minute hold.
- [ ] Check Kraken's current futures fee tier before trusting the default fees.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
