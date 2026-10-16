# 0009. Record funding, mark, index, open interest, and liquidations in the recorder, and read them the same way live and in replay

**Date**: 2026-10-11 (amended 2026-10-16: holes at a run's tail and a starved event loop, AC-6, AC-7, AC-17, AC-18, Milestone 5)
**Status**: In Progress

## Summary

The recorder starts recording the BTC perp's own market context next to the order book: the funding rate, the mark and index prices (their gap is the basis), open interest (how many contracts are open), and liquidations (positions the exchange closed by force). Funding, mark, and index come from NautilusTrader's Kraken adapter. Open interest and liquidations it does not offer, so one small public websocket client reads them from Kraken directly. Everything lands in the same hourly Parquet catalog, and one pure reader in core answers "what was known at this moment", the same way in the trader, in a replay, and in tests, so a backtest can never see the future. It also lets the scalping simulator charge funding on our own recorded days.

The 2026-10-16 amendment closes a hole the first VPS day found: on 2026-10-15 open interest went silent for 67 minutes until the recorder restarted, and no gap row said so. Two things went wrong. The node's event loop was starved (busy with the data queue), so the feed's idle timer never fired. And the gap rules only saw an outage once it ended. Now the feed writes an outage line the moment it goes down, the compactor closes any line still open, and records a "tail silence" (no rows from a stream's last row to the end of the run). The feed also warns when its messages arrive late, the early sign of a starved loop.

## Requirements

**User stories**:
- As the trader, I want funding, basis, open interest, and liquidations recorded from now on, because most of this history cannot be downloaded later and strategies need weeks of it to be tested.
- As the trader, I want a strategy to read these values in a replay exactly as they were known at each moment, so a regime filter that looks good in a backtest is not secretly using the future.
- As the trader, I want the live trader to read the very same values through the very same code, so a strategy that uses them runs unchanged from replay to paper.
- As the trader, I want my own replay days to include funding, so their results stop being labelled "before funding".
- As the trader, I want a broken context feed never to cost me order book data or a trade, because the book is the irreplaceable part.

**Acceptance criteria**:
- **AC-1**: For each id in `RECORDER_INSTRUMENTS`, the recorder subscribes to Nautilus `MarkPriceUpdate`, `IndexPriceUpdate`, and `FundingRateUpdate`. `RECORDED_TYPES` (`apps/recorder/src/recorder/main.py`) also lists those three and the two custom types `OpenInterestUpdate` and `LiquidationTick` (AC-3), so the streaming writer writes all five into `live/<run>/` with the same hourly rotation as the book.
- **AC-2**: The compactor converts each closed hour of the five context streams into `data/<table>/<instrument>/<interval>.parquet` (the table name is what `class_to_filename` gives for the class; the Milestone 0 gate records the two custom names), through the same reorder window, `HourSink`, and `done.json` commit point as deltas and trades. Rows are sorted by `ts_init` with a stable sort, so rows at the same nanosecond keep their arrival order. Every update is kept as received, at the precision the adapter gives it (no rounding of our own): no row is dropped or merged, except the liquidation duplicates of AC-5. Rows that arrive later than the reorder window go to the hour's quarantine file and the stream's `incomplete_hours` in the manifest, as for trades. A rerun of a killed pass writes identical bytes (spec 0007 AC-11 holds for the new tables). An hour with no rows for a stream writes no file for it.
- **AC-3**: `ContextFeedActor` (`trading_core.node.context_feed`) opens one public websocket to `CONTEXT_WS_URL` with the `websockets` library, as a task on the node's event loop, and subscribes to the `ticker` and `trade` feeds for the configured product ids (each instrument id without its `.KRAKEN` venue suffix, so `PF_XBTUSD.KRAKEN` is `PF_XBTUSD`). It sends no credentials. A message for an instrument not yet in the node's cache is skipped as `context_message_bad` with reason `instrument_not_loaded`. Each `ticker` message with an `openInterest` becomes an `OpenInterestUpdate` (instrument id, `value` as a `Quantity` in contracts at the instrument's size precision, `ts_event` from Kraken's `time` in ms, `ts_init` from the actor's clock on receipt). Each `trade` or `trade_snapshot` row whose `type` is `liquidation`, `termination`, or `block` becomes a `LiquidationTick` (instrument id, `trade_id` from Kraken's `uid`, `kind`, `aggressor_side` from Kraken's `side`, `price`, `size`, `ts_event`, `ts_init`). Rows of type `fill` are ignored (the adapter already records them as `TradeTick`). The actor publishes each object on the message bus as custom data with `DataType(cls, metadata={"instrument_id": <id>})`, the same metadata every subscriber and the backtest engine use, where the streaming writer and any subscriber receive it. The outages file path is a constructor argument (a callable giving `live/<instance id>/context_outages.jsonl` in the recorder, `None` in the trader), as `RecordingActor` takes `run_id`, not a config field.
- **AC-4**: A message the actor cannot parse (bad JSON, a missing or non numeric field, an unknown trade `type`, an unknown product id) is skipped and logged once per minute per reason as `context_message_bad` with `feed`, `reason`, and the count since the last log. Nothing raised in parsing or publishing leaves the actor's task: the task logs it and carries on.
- **AC-5**: A `trade_id` is never published twice by one actor run (a resubscribe resends recent trades as `trade_snapshot`): the actor keeps the last 10 000 liquidation uids it published and drops a repeat. The compactor drops a `liquidation_tick` row whose `trade_id` already appears in the same hour or the hour before (a restart near the hour mark), logs `liquidation_duplicate` with the count, and keeps the row with the earliest `ts_init`. The previous hour's uids per instrument are kept in `done.json`, so a killed pass reruns to the same result.
- **AC-6**: The connection uses `websockets` pings (20 s interval, 20 s timeout), and the actor forces a reconnect when no message at all has arrived for `CONTEXT_IDLE_S`, so a silent stall counts as an outage. When the websocket closes, errors, or stalls, the actor logs `context_feed_down` with the reason, reconnects with backoff (1, 2, 4, ... up to `CONTEXT_RECONNECT_MAX_S`, reset after 60 s connected), resubscribes, and logs `context_feed_up` with the outage seconds. Each outage gives two lines in `live/<run>/context_outages.jsonl` (amended 2026-10-16). On entering `down` it appends an open line `{"start_ns", "end_ns": null, "reason"}`, where `start_ns` is the receive time of the last websocket message of any kind (data, subscription reply, or heartbeat) before the outage. On `context_feed_up` it appends the closed line `{"start_ns", "end_ns", "reason"}` with the same `start_ns` and reason, and `end_ns` the receive time of the first message of any kind after it. A drop before the run's first message writes no line (there is no `start_ns`). `start_ns` is unique per outage within a run, since a new outage starts at or after the previous one's `end_ns`, so it pairs an open line with its closed line. A failed append logs `context_outage_write_failed` and nothing else: no context failure stops the recorder, changes its health check, or makes autoheal restart it.
- **AC-7**: The compactor writes context gaps into the existing `gaps/<day>.parquet` with a new `stream` column (the table name; rows from before this spec read as `order_book_deltas`). `stream` joins `Gap`, its sort key (start, stream, instrument, kind), the `write_gaps` dedupe key, `GAP_SCHEMA`, and the manifest's gap totals; `read_gaps` takes a stream filter that defaults to `order_book_deltas`. The kinds:
  - `recorder_down` for all five streams, from the book stream's run boundaries (the last book row of one run to the first book row of the next), never from the context rows themselves, so a quiet liquidation stream gives no false gap;
  - `silence` when two rows of one run of `mark_price_update`, `index_price_update`, or `open_interest_update` are `CONTEXT_SILENCE_S` or more apart, with the per stream detector state kept in `done.json` like `GapState`. Also a tail silence (amended 2026-10-16): from such a stream's last row of a run to the run's end, or to the start of that run's tail `reconnect` (below) for open interest when that comes first, when the span is `CONTEXT_SILENCE_S` or more. It ends at that `reconnect` so the overlap rule does not drop it whole. A stream with no row at all in the run, or whose last row is at or after the run's end, gets no tail silence (an hour with no rows has no file, which AC-17 checks);
  - `reconnect` for `open_interest_update` and `liquidation_tick` from each closed `context_outages.jsonl` line. Amended 2026-10-16: an open line whose `start_ns` never gets a closed line is closed at the run's end, so an outage still open when the recorder stopped is recorded (a tail `reconnect`, with no `CONTEXT_SILENCE_S` minimum). An open line with a closed partner gives nothing of its own. An open line starting at or after the run's end gives nothing. Files with closed lines only (written before the amendment) read as before, and a closed line without an open partner is valid.

  **Tail gaps** (amended 2026-10-16). A run's end, per instrument, is the `ts_init` of the run's last book row: the `start_ns` of the `recorder_down` gap the book writes when its next run begins. A tail gap ends there, touching `recorder_down` without overlapping it, so both are kept. Three `done.json` maps hold what a tail waits on, so nothing depends on the run folder still existing (the compactor deletes a converted run's folder) and a killed pass reruns to identical bytes. A missing key reads as empty.
  - `run_ends` (instrument, then run id, to end ns). Saved in the same pass and commit as that `recorder_down`.
  - `open_outages` (run id to a list of `{start_ns, reason}`). An open line is copied in when `_outage_gaps` first reads it (the `done.outages` line counter moves past it as now). A closed line read later, in any pass, removes its stored partner.
  - `pending_tails` (stream key, then run id, to that stream's last row `ts_init` in the run). Set when a silence stream ends a run.
  
  A resolve step runs at the end of every pass, after every stream and before `done.json` is saved. For each run with a known end, it writes the tail `reconnect` rows of its stored open lines, then the tail silences, and removes the resolved entries. So the gap rows are the same whichever stream a pass converts first. A `run_ends` entry is removed once no open line or pending tail of that run is left and every silence stream has passed that run. A run with no book rows has no end of its own; it lies inside the `recorder_down` around it, which already covers its context, so its entries are dropped when the `recorder_down` that spans it is written. While a run is current, or its successor has not started yet (the recorder is still down, or `--offline` reaches the last run), its tail waits, exactly as `recorder_down` waits. A tail gap that lands in a day whose manifest is already written rewrites that day's manifest, as late counts do. Tail gaps never touch the book's `done.empty` or `incomplete` hours.

  Where spans overlap within one stream, `recorder_down` wins over `reconnect`, which wins over `silence`. Liquidations and funding get no `silence` gaps (quiet spells are normal). The daily manifest adds per stream row counts and gap seconds. The lab's segment planner reads only `order_book_deltas` gaps, so a context gap never splits a replay segment.
- **AC-8**: `ContextBook` (`trading_core.marketdata.context`, pure, `Decimal` and nanosecond ints only) folds updates in arrival order and answers `snapshot(now_ns) -> MarketContext`. `MarketContext` has `mark`, `index`, `basis`, `funding_rate`, `open_interest`, each a `ContextValue(value, known_ns, is_stale)` or `None` when never seen, plus `next_funding_ns` (or `None`) and `liquidations`. `basis = (mark − index) ÷ index` from the latest of each, computed in an explicit `decimal` context of 28 digits with `ROUND_HALF_EVEN`, with `known_ns` the later of the two and stale when either is. An update older (by `ts_init`) than the latest of its kind is ignored. `snapshot` with a `now_ns` earlier than any applied `ts_init` raises `ContextLookAheadError`. The book's max ages and liquidation window are constructor arguments; it reads no settings itself.
- **AC-9**: A value is stale when `now_ns − known_ns` exceeds its max age from settings (`CONTEXT_MAX_AGE_MARK_S`, `CONTEXT_MAX_AGE_INDEX_S`, `CONTEXT_MAX_AGE_OPEN_INTEREST_S`, `CONTEXT_MAX_AGE_FUNDING_S`). Strategies that use a value must treat a stale or missing one as missing (fail closed); none does yet.
- **AC-10**: `MarketContext.liquidations` is a `LiquidationWindow` with `sold_size`, `sold_notional` (`aggressor_side` SELL, longs forced out), `bought_size`, `bought_notional` (BUY, shorts forced out), each summed over the window `(now_ns − CONTEXT_LIQUIDATION_WINDOW_S, now_ns]` with notional `price × size` in exact `Decimal` USD; `last`, the newest liquidation seen at all (or `None`); and `known`. `known` is true only when the book was built with `liquidations_available=True` (live, and `own` source replays; false for `tardis`, which has no liquidation data) and `open_interest` is present and not stale (both come over the same connection, so a fresh open interest means the liquidation feed is up). A strategy must treat `known == False` as missing, never as calm. Only `kind == liquidation` counts in the sums; `termination` and `block` are recorded but not summed. Rows older than the latest applied `ts_init` minus the window are pruned when an update is applied, never in `snapshot`, so a snapshot's result never depends on earlier snapshot calls.
- **AC-11**: `KfsStrategy` (`trading_core.node.strategy`) gains `use_market_context` in its config (default false). When true, `KfsStrategy.on_start` subscribes to mark, index, and funding for its instrument and to the two custom data types, its `on_mark_price`, `on_index_price`, `on_funding_rate`, and `on_data` handlers fold every update into a `ContextBook`, and `self.market_context()` returns `snapshot(clock.timestamp_ns())`. Subclasses that override these hooks call `super()` first; `PlaceholderTimed.on_start` is changed to do so. Live, a `ContextLookAheadError` (two clocks a hair apart) is caught inside `market_context()`, logged as `context_clock_behind` once per minute, and the snapshot is taken at the latest applied `ts_init`; in a replay it is raised. The max ages and liquidation window come from the trader's `ContextSettings` live and from the run file in the lab (AC-13). `PlaceholderTimed` sets `use_market_context` true in the trader's settings and logs `market_context` (all fields, Decimals as strings, ages in seconds) once per bar. No entry or exit rule reads it.
- **AC-12**: The trader node adds a `ContextFeedActor` with the same settings as the recorder. The trader records none of the context (it has no streaming writer). A context outage in the trader only logs (AC-6); it never blocks entries, pauses, or alerts.
- **AC-13**: The lab's hour stream (`apps/lab/src/lab/stream.py`) also reads each hour's five context tables and merges them into the engine's data by `ts_init`. At the same nanosecond the order is book, trades, then `mark_price_update`, `index_price_update`, `funding_rate_update`, `open_interest_update`, `liquidation_tick`, and within one table the file order. Each segment first feeds the context rows (only) with `ts_init` in `[segment start − warmup_s, segment start)`, so funding and open interest are known from the first book row. A replayed `KfsStrategy` with `use_market_context` therefore receives them through the same handlers as live. The run file gains an optional `[context]` table (`warmup_s` default 7 200, the four max ages, `liquidation_window_s`, with the `ContextSettings` defaults) that is copied into `run_config.context`, so replay results never depend on the laptop's environment. `liquidations_available` is true for `own` runs and false for `tardis` runs. For any replay time `t` in a segment, `market_context()` equals a `ContextBook` fed only the catalog rows with `ts_init` in `[segment start − warmup_s, t]`.
- **AC-14**: An `own` source replay applies funding when, for every funding boundary inside its segments, the last own `funding_rate_update` row at or before the boundary (as `trading_core.lab.funding.rate_at` picks it) has `ts_init` in `(boundary − FUNDING_PERIOD_NS, boundary]` (the existing funding rules otherwise, rate as stored). The parent checks this once, before any unit starts, from `catalog.funding_rates`; `apps/lab/src/lab/replay.py` stops tying funding to `run.source == "tardis"` in the unit plan, the batch's `funding_rates`, `before_funding`, and `_run_config`. Then `run_config.funding_source` is `"own"`, funding rows and totals are written as for Tardis runs (spec 0008 AC-9), and the report has no "before funding" label. If any boundary lacks such a row, the whole run applies none: `funding_source` stays `"none"`, the label stays, and the report says `funding skipped: no rate before <first such boundary, UTC>`.
- **AC-15**: `lab import-tardis` also writes `mark_price_update`, `index_price_update`, and `open_interest_update` hour files from each day's `derivative_ticker`, one row per CSV row that has the value, and from now on writes `funding_rate_update` for every row too (not only changes, so its staleness matches own data; replay results do not change, since `rate_at` picks the same rate). `local_timestamp` (µs) × 1 000 is `ts_init` and `timestamp` (µs) × 1 000 is `ts_event`; prices use the precision the gate records for the adapter's mark and index. All of it is written before the day's manifest (its commit point). `lab import-tardis --context <from> <to>` backfills days that already have a manifest: it downloads only `derivative_ticker`, rewrites `funding_rate_update` and writes the three tables, never touches the day's gap file, and writes `manifests/context/<day>.json` (`day`, and per table `rows` and `bytes`) last as that step's commit point, in a subfolder so the day manifest globs never read it. Both reruns give identical bytes, and a day already done logs `lab_import_day_skipped`.
- **AC-16**: Retention and backup (spec 0007 AC-6, AC-8) cover the five new tables with the same hour rule, unchanged. Nothing about the context goes into Postgres. The recorder still has no secrets and no execution client, and the trader's canary leak test still passes.
- **AC-17**: On the VPS, over 24 h after deploy: every closed hour has `mark_price_update`, `index_price_update`, `funding_rate_update`, and `open_interest_update` files, the gap files list every outage, and a forced websocket drop gives `context_feed_down`, `context_feed_up`, and one `reconnect` gap for the two raw streams while the book records on without a gap. An outage still open when the recorder stops gives a `reconnect` gap for the two raw streams that ends where the run's `recorder_down` starts (amended 2026-10-16). The measured rows and bytes per day per table are written into verify.md.
- **AC-18** (amended 2026-10-16): For every open interest record, the feed computes its lag, `ts_init − ts_event` (how late our node handled what Kraken sent). When the lag is over `CONTEXT_LAG_WARN_S`, it logs `context_feed_lagging` with `lag_s` (the largest lag since the last such log, 3 decimals) and `count` (records over the limit since then), at most once per minute, in the recorder and the trader alike. A negative lag (a clock a hair apart) counts as zero. The warning changes nothing else: no reconnect, no gap row, no health or trading effect. It is the early sign that the node's event loop is starved, which also stalls the idle watchdog of AC-6 (see the rationale's amendment). The counting rule is pure (`LagWatch` in `trading_core.marketdata.context_feed`, beside `BadCounter`).

## Decision

**Chosen option**: Option 2: native Nautilus types plus one raw public websocket actor, all in the existing recorder.

Record mark, index, and funding through Nautilus's own Kraken subscriptions, record open interest and liquidations through a thin `websockets` actor as two registered custom data types, carry all five through the existing streaming writer and compactor, and read them everywhere through one pure `ContextBook` keyed on `ts_init`.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch** (all in the Parquet catalog, spec 0007 layout `data/<table>/PF_XBTUSD.KRAKEN/<interval>.parquet`; nothing in Postgres):

| Table | Class | Fields (all required unless noted) | Rules |
|---|---|---|---|
| `mark_price_update` | Nautilus `MarkPriceUpdate` | instrument_id, value (Price), ts_event, ts_init | every update as received |
| `index_price_update` | Nautilus `IndexPriceUpdate` | instrument_id, value (Price), ts_event, ts_init | every update as received |
| `funding_rate_update` | Nautilus `FundingRateUpdate` | instrument_id, rate (Decimal, fractional per period, meaning confirmed in the gate), next_funding_ns (nullable), ts_event, ts_init | every update as received, for own and (from this spec) Tardis data alike |
| custom, name from the gate | `OpenInterestUpdate` (new, `trading_core.node.context_types`) | instrument_id, value (Quantity, contracts), ts_event, ts_init | every `ticker` push |
| custom, name from the gate | `LiquidationTick` (new, same module) | instrument_id, trade_id (str, Kraken uid), kind (`liquidation`, `termination`, `block`), aggressor_side (BUY, SELL), price (Price), size (Quantity), ts_event, ts_init | `trade_id` unique per instrument across the catalog (AC-5); matches a `trade_tick.trade_id` only if the gate shows the adapter also emits non fill trades as `TradeTick` (otherwise no join is claimed) |
| `gaps/<day>.parquet` (existing) | | adds `stream` (str) | old rows read as `order_book_deltas` |
| `live/<run>/context_outages.jsonl` (new side file) | | start_ns, end_ns (null on the open line), reason | compactor input only, like `sequence_holes.jsonl`; an open and a closed line per outage, paired by `start_ns` (AC-6) |
| `done.json` (existing) | | gains `run_ends` {instrument: {run: end_ns}}, `open_outages` {run: [{start_ns, reason}]}, `pending_tails` {stream key: {run: last_ts}}; missing reads as empty, written sorted like the other maps | compactor state only; entries removed once resolved; kept so a rerun gives identical bytes and the run folder may go (AC-7) |

Each row relates N:1 to the hour's `crypto_perpetual` row. The custom classes are registered with Nautilus's custom data mechanism (an Arrow schema), the gate proves they stream to feather and read back; their Decimal fields are serialized as strings or fixed point, never floats. The pure core types carry no Nautilus import: the node module converts Nautilus objects to `trading_core.marketdata.context` inputs (`Decimal`, ns ints, plain enums).

**State transitions**: `ContextFeedActor` connection: `connecting → subscribed → down → connecting …`, `stopped` on node stop. Logs on `subscribed` after `down` (`context_feed_up`) and on entering `down` (`context_feed_down`).

**API surface** (in process interfaces; no network API is added):

| Interface | Kind | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `ContextFeedActor(ContextFeedConfig)` | Nautilus Actor | `ws_url`, `instrument_ids`, `reconnect_max_s`, `outages_path` (callable, recorder only) | publishes `OpenInterestUpdate`, `LiquidationTick` | none (public feed) | never raises out of its task (AC-4, AC-6) |
| `ContextBook(max_ages, liquidation_window_ns, liquidations_available)` | pure class | `apply_mark/apply_index/apply_funding/apply_open_interest/apply_liquidation` (Decimals, ns) | `snapshot(now_ns) -> MarketContext` | n/a | `ContextLookAheadError` (AC-8) |
| `KfsStrategy.market_context()` | method | none (strategy clock) | `MarketContext` | n/a | `RuntimeError` if `use_market_context` is false |
| compactor context streams | batch | feather files, `context_outages.jsonl` | hour files, gap rows, manifest counts | n/a | `FeatherUnreadable` fails the pass, as for the book |
| `lab replay` own funding | CLI, existing | run file | funding rows or the skip line (AC-14) | n/a | none new |
| `lab import-tardis [--context] <from> <to>` | CLI, existing | UTC days | three context tables, `<day>.context.json` | n/a | exit 64 for a non first of month day, as now |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| record mark, index, funding | value, rate, next_funding_ns, ts_event | Nautilus Kraken adapter (`subscribe_mark_prices`, `subscribe_index_prices`, `subscribe_funding_rates`) |
| record any context row | ts_init | the receiving node's clock (Nautilus sets it for native types; `ContextFeedActor` uses `clock.timestamp_ns()`) |
| `OpenInterestUpdate.value` | open contracts | Kraken `ticker.openInterest`, at the instrument's `size_precision` from the cache |
| `OpenInterestUpdate.ts_event`, `LiquidationTick.ts_event` | exchange time | Kraken `time` (ms) × 1 000 000 |
| `LiquidationTick` fields | trade_id, kind, aggressor_side, price, size | Kraken `trade`/`trade_snapshot`: `uid`, `type`, `side`, `price`, `qty` (the gate confirms `side` is the aggressor) |
| `MarketContext.basis` | `(mark − index) ÷ index` | derived on read from the latest mark and index (AC-8) |
| `ContextValue.is_stale` | stale flag | `now_ns − known_ns` against the `CONTEXT_MAX_AGE_*` settings (AC-9) |
| `liquidations` sums | sold and bought size and notional | derived from `LiquidationTick` rows of kind `liquidation` in the window (AC-10) |
| context gap rows | start, end, kind, stream | compactor: run boundaries, `CONTEXT_SILENCE_S`, `context_outages.jsonl` (AC-7) |
| tail silence, closed open outage | end_ns | the run's end: `start_ns` of the book's `recorder_down` at that boundary, kept in `done.json` `run_ends` (AC-7) |
| tail silence | start_ns | the stream's last row of the run, from its silence detector state, kept in `done.json` `pending_tails` (AC-7) |
| tail silence (open interest) | end_ns when a tail `reconnect` exists | the earlier of the run's end and that run's unpaired open line `start_ns` (AC-7) |
| tail `reconnect` | start_ns, reason | the unpaired open line, kept in `done.json` `open_outages` (AC-7) |
| open outage line | start_ns, reason | the feed's last message `ts_init` and the down reason (AC-6) |
| `context_feed_lagging` | lag_s, count | `ts_init` (actor clock) minus the record's `ts_event` (Kraken `time`), folded by `LagWatch` (AC-18) |
| own run funding | rate at each boundary | own `funding_rate_update` rows via `apps/lab/src/lab/catalog.py` `funding_rates` and `trading_core.lab.funding.rate_at`, with the one period freshness rule (AC-14) |
| own run funding payment mark | mark at the boundary | the mid at that instant, unchanged from spec 0008 AC-9 (the recorded mark is not used, so own and Tardis runs price funding the same way) |
| Tardis context rows | mark, index, open interest | `derivative_ticker` CSV columns `mark_price`, `index_price`, `open_interest` parsed as `Decimal` (AC-15) |
| `market_context` log line | all fields | `KfsStrategy.market_context()` |

**Key invariants**:
- Known time is `ts_init`, always. No reader uses `ts_event` to decide what was known.
- `ContextBook.snapshot(t)` depends only on updates with `ts_init ≤ t` (AC-8, AC-13).
- No float touches a context value: `Decimal`, `Price`, `Quantity` only (the AGENTS.md money rule).
- Context never stops the recorder, fails its health check, splits a replay segment, or changes a trading decision in this feature.
- `trade_id` of `liquidation_tick` is unique per instrument in the catalog.
- No context hole goes unrecorded: every span without context rows inside a run is a `silence` or `reconnect` gap, including one still open when the run ends; between runs it is `recorder_down`. A tail gap ends where `recorder_down` starts, never overlapping it.
- The feed shares the node's event loop with Nautilus's data queue reader, which does not yield while its queue holds items. Its timers are therefore only as live as that queue is short; the lag warning (AC-18) and the tail rules (AC-7) cover the case where they are not.
- Pure logic (book fold, staleness, window sums, gap rules, outage parsing, dedupe rule) lives in `trading_core.marketdata`; the actor, the custom type classes, and the strategy hooks live in `trading_core.node`; the recorder and trader files only wire them.

**Security model**: public market data only. No credentials are sent or loaded; the recorder keeps no secrets and no execution client (env guard unchanged). The websocket URL comes from validated settings, and only `wss://` is accepted. No personal or regulated data. Nothing is written to Postgres or Redis.

**Configuration required** (a `ContextSettings` mixin in `trading_core.settings`, used by `RecorderSettings`, the trader settings, and the compactor's settings for the silence rule; validated at startup):
- `CONTEXT_WS_URL`: Kraken Futures public websocket, default `wss://futures.kraken.com/ws/v1`, must start with `wss://`.
- `CONTEXT_RECONNECT_MAX_S`: backoff ceiling, default 60, at least 1.
- `CONTEXT_IDLE_S`: no message for this long forces a reconnect, default 30, at least 5.
- `CONTEXT_SILENCE_S`: compactor silence gap for mark, index, open interest, default 60.
- `CONTEXT_MAX_AGE_MARK_S`, `CONTEXT_MAX_AGE_INDEX_S`, `CONTEXT_MAX_AGE_OPEN_INTEREST_S`: default 30 each; `CONTEXT_MAX_AGE_FUNDING_S`: default 7 200. The gate measures each stream's real cadence; a default below three times the measured interval is raised in this spec before Milestone 1.
- `CONTEXT_LIQUIDATION_WINDOW_S`: default 60, at least 1.
- `CONTEXT_LAG_WARN_S` (amended 2026-10-16): open interest lag over this logs `context_feed_lagging`, default 10, at least 1 (normal lag is about 0.2 s, the worst spike since spec 0012 about 3.7 s, 105 s before the 2026-10-15 hole). It has a default, so `/etc/kfs/kfs.env` needs no new line.
- The lab takes none of these from its environment: the run file's `[context]` table carries `warmup_s` (default 7 200), the four max ages, and `liquidation_window_s` (AC-13).
- New pinned dependency: `websockets` in the core `node` extra (latest release at build time, pinned exactly, locked with `uv lock`).

**Critical test scenarios**:
- Happy path: a recorded feather hour with all five streams converts to five Parquet files sorted by `ts_init`, and a rerun gives identical bytes, verifies **AC-1**, **AC-2**
- Parsing: recorded Kraken `ticker`, `trade`, `trade_snapshot` messages (fixtures captured in the gate) give the expected objects; `fill` rows give none; a bad message is logged once and skipped, verifies **AC-3**, **AC-4**
- Duplicates: a resubscribe snapshot repeating three uids publishes none of them again; two runs straddling an hour with a shared uid keep one row, verifies **AC-5**
- Failure case: a fake websocket server that drops the connection, and one that goes silent past `CONTEXT_IDLE_S`, each give the backoff sequence, the two log lines, one outage line starting at the last message, and one `reconnect` gap, while the recorder's health check still passes, verifies **AC-6**, **AC-7**
- Tail hole (amended): a run whose open interest stops 10 minutes before its last book row gives a tail `silence` from the last open interest row to that row, kept beside the `recorder_down` that starts there; an open outage line with no closed partner gives `reconnect` for both raw streams over the same span; an open line with a closed partner gives one gap only; a tail under `CONTEXT_SILENCE_S` gives none; the same rows come out whether the book or the context stream converts first in the pass, and a pass killed after `run_ends` is saved but before the tail is written reruns to identical bytes; an old file with closed lines only gives the same gaps as before; a closed line arriving a pass after its open line gives one gap; the run folder deleted before the next run's `recorder_down` still gives the tail gaps; an open line in the current run gives no gap yet; a run whose open interest stopped 5 minutes before its last message keeps a tail silence ending at the tail `reconnect`; a tail whose run ended at 23:50 with the day's manifest already written rewrites that manifest; a run with no book rows between two others leaves only the spanning `recorder_down`, verifies **AC-6**, **AC-7**
- Starved feed (amended): a fake server whose messages the feed handles 20 s late logs `context_feed_lagging` once per minute with the largest lag and the count; a lag of exactly `CONTEXT_LAG_WARN_S` does not log; a negative lag counts as zero; nothing reconnects, verifies **AC-18**
- Gaps: two runs with a quiet liquidation stream give `recorder_down` only between the book runs; five streams' `recorder_down` rows with one start all survive the dedupe; `read_gaps` with no filter returns only book gaps; an overlapping `reconnect` and `silence` keep only the `reconnect`, verifies **AC-7**
- Warmup: a segment starting at 00:00 with a funding row at 23:30 the day before reads that rate at the first book row, verifies **AC-13**
- Liquidations unknown: a stale open interest, or a `tardis` run, gives `known == False` with zero sums, verifies **AC-10**
- Look ahead: a `ContextBook` fed rows up to `t` equals the replay's `market_context()` at `t`; `snapshot` before an applied `ts_init` raises `ContextLookAheadError`; an older update is ignored, verifies **AC-8**, **AC-13**
- Staleness and basis: mark fresh and index stale gives a stale basis; a value one ns past its max age is stale, verifies **AC-8**, **AC-9**
- Liquidation window: a liquidation exactly `window` old is out, one ns newer is in; `termination` is recorded but not summed, verifies **AC-10**
- Own funding: a run with rates at every boundary pays funding with source `own`; a run missing one boundary pays none and prints the skip line, verifies **AC-14**
- Tardis: one Tardis day writes the three context tables; `--context` on a done day writes them and the context manifest; reruns are byte identical, verifies **AC-15**
- Boundaries: `test_core_boundary.py` still passes (no Nautilus in `marketdata`), the recorder env guard test and the canary leak test pass, verifies **AC-16**
- Auth/permission: the actor's config has no key fields and its connect sends no auth message; a non `wss://` URL refuses startup, verifies **AC-16**

## Build plan

Tracer Bullet: Milestone 1 pushes one stream (funding) through every layer, recorder to catalog to the lab and the strategy reader, before the others widen it.

**Milestone 0: Gate** (answers that change the build; record each in verify.md)
1. In the recorder image, subscribe to mark, index, funding, and trades on `PF_XBTUSD` for 15 minutes: measure each stream's cadence and the price precision the adapter gives mark and index, check whether non fill trades also arrive as `TradeTick`, and confirm whether `FundingRateUpdate.rate` is Kraken's relative (fractional) rate or the absolute one, and the funding period. If it is the absolute rate, stop and amend AC-14 before Milestone 1. Satisfies **AC-1**, **AC-9**, **AC-14**
2. With `websockets`, read Kraken's `ticker` and `trade` feeds for 30 minutes: confirm `openInterest`, the `type` values, that `side` is the aggressor, and the `trade_snapshot` on subscribe. Save raw messages as test fixtures. If no `liquidation` type is seen, check Kraken's docs; if the flag is gone, liquidations drop out (AC-3, AC-5, AC-10 marked dropped) as this spec allows. Satisfies **AC-3**, **AC-5**
3. Prove a registered custom data class streams through `StreamingConfig` to feather, the compactor's `read_chunks` reads it, `HourSink` writes Parquet, and `ParquetDataCatalog` and the `BacktestEngine` read it back with Decimal values intact. If the writer cannot stream it, switch AC-1 for the two custom types to a `live/<run>/context_<table>.jsonl` side file the compactor converts, and amend this spec. Satisfies **AC-1**, **AC-2**, **AC-13**
4. Estimate rows and bytes per day per stream from tasks 1 and 2. Satisfies **AC-17**

**Milestone 1: Thin thread, funding end to end**
5. `ContextSettings` mixin and its validation. Satisfies **AC-9**, **AC-16**
6. Recorder subscribes to `FundingRateUpdate`; `RECORDED_TYPES` and `STREAM_TABLES` gain `funding_rate_update`; the compactor converts it, with reorder, `done.json`, and rerun identity. Satisfies **AC-1**, **AC-2**
7. Pure `ContextBook` and `MarketContext` with funding only, staleness, and `ContextLookAheadError`. Satisfies **AC-8**, **AC-9**
8. `KfsStrategy.use_market_context` for funding; `PlaceholderTimed` logs `market_context`. Satisfies **AC-11**
9. Own replay funding, all or nothing, with the skip line. Satisfies **AC-14**
10. Gap `stream` column, old rows as `order_book_deltas`, segment planner reads deltas gaps only; `recorder_down` for funding; manifest counts. Satisfies **AC-7**

**Milestone 2: Mark, index, basis**
11. Recorder and compactor for `mark_price_update` and `index_price_update`, with `silence` gaps. Satisfies **AC-1**, **AC-2**, **AC-7**
12. `ContextBook` mark, index, and the derived basis; strategy handlers. Satisfies **AC-8**, **AC-9**, **AC-11**
13. Lab hour stream merges the three native context tables into the engine in the fixed tie order, with the segment warmup, the run file `[context]` table copied into `run_config.context`, and the look ahead equality test. Satisfies **AC-13**

**Milestone 3: Raw client, open interest and liquidations**
14. `websockets` dependency (`uv lock`), custom types `OpenInterestUpdate` and `LiquidationTick`, pure parsers over the gate fixtures, `context_message_bad`. Satisfies **AC-3**, **AC-4**
15. `ContextFeedActor`: connect, subscribe, publish, uid dedupe, backoff, the two log lines, `context_outages.jsonl`; a fake server test. Satisfies **AC-3**, **AC-5**, **AC-6**
16. Recorder wires the actor; compactor converts both tables, drops cross hour duplicates, writes `reconnect` gaps from the outages file. Satisfies **AC-1**, **AC-2**, **AC-5**, **AC-7**
17. `ContextBook` open interest and the liquidation window; strategy handlers for custom data; the lab stream adds both tables. Satisfies **AC-8**, **AC-10**, **AC-11**, **AC-13**

**Milestone 4: Live read, Tardis history, deploy**
18. Trader adds `ContextFeedActor`; context outages only log. Satisfies **AC-12**
19. `lab import-tardis` writes mark, index, open interest, and every funding row; `--context` backfill with `manifests/context/<day>.json`. Satisfies **AC-15**
20. Retention and backup checks over the new tables; boundary, env guard, and canary tests. Satisfies **AC-16**
21. Deploy to the VPS, the forced drop, the 24 h check, and the measured sizes in verify.md. Satisfies **AC-17**

**Milestone 5: Holes at a run's tail and a starved loop** (amended 2026-10-16, from verify.md step 5 and its `/debug`). A thin thread first: the pure rules, then the feed, then the compactor, then the VPS.
22. Pure rules in `trading_core.marketdata`: `Outage` with an optional `end_ns`, the open line, `parse_outages` reading both kinds and pairing them by `start_ns`; closing unpaired open lines at a given run end; the tail rule on `SilenceDetector` (a run's last row against its end); `LagWatch`. Satisfies **AC-6**, **AC-7**, **AC-18**
23. Feed: the open line on entering `down`, `LagWatch` in `_received` for open interest records, `CONTEXT_LAG_WARN_S` in `ContextSettings`. Satisfies **AC-6**, **AC-18**
24. Compactor: `done.json` gains `run_ends`, `open_outages`, `pending_tails`; the book saves each ended run's end where it writes `recorder_down`; `_outage_gaps` stores open lines and removes them on their closed line; silence streams store their last row on a run end; the end of pass resolve step writes tail `reconnect` and tail silence rows, prunes, and rewrites a closed day's manifest; the order, rerun, deleted folder, and midnight tests. Satisfies **AC-7**
25. Deploy no earlier than 2026-10-16 18:40 UTC (spec 0012's 24 h AC-8 check runs until then), then verify.md step 12. Satisfies **AC-17**, **AC-18**

## Consequences

**Positive**:
- The context history clock starts now, for the inputs that cannot be bought back later (open interest and liquidations on our own feed).
- One reader, keyed on `ts_init`, makes the same value appear at the same moment in replay and live, which removes the most common way a regime filter flatters a backtest.
- Own replay days get funding, so own and Tardis results stand on one cost basis.
- About 55 Tardis days get mark, index, and open interest history for free.

**Negative / tradeoffs**:
- A second websocket connection per node, and our own reconnect code to own, because Nautilus does not expose open interest or liquidations. A Nautilus upgrade that adds them should replace the actor.
- One more pinned dependency (`websockets`).
- The gap file gains a column, and every gap reader must filter by `stream`.
- Context outages stay quiet until feature 12 alerts on `context_feed_down`; a long outage is only visible in the logs and the gap files.
- Recording every update costs more disk than change only rows (the gate measures it; expected small next to the book).
- The feed still shares the event loop with a queue reader that can starve it (amendment). The tail rules record the hole and the lag warning shows it coming, but neither stops it; only keeping the data queue short does (spec 0012). Feature 12 has to alert on `context_feed_lagging` before anyone hears of it.
- A tail gap or a closed open line waits for the run's end, so a hole in the live run only shows once that run is over or the feed recovers. If the closing append fails, the open line is closed at the run's end and the gap is longer than the real outage (the safe direction).
- Old compactor code would count an open line as unreadable, so the recorder and compactor ship together (one image, one deploy).
- The live funding rate Kraken publishes is the current estimate for the coming period; own funding uses the last rate at or before each boundary, the same simplification the Tardis runs already make.

**Neutral**:
- New log codes: `context_feed_down`, `context_feed_up`, `context_message_bad`, `context_outage_write_failed`, `context_clock_behind`, `liquidation_duplicate`, `market_context`, and `context_feed_lagging` (amendment).
- Rerunning an older own replay run file now applies funding where the data allows it; `run_config.funding_source` and `run_config.context` show the difference.
- No migration: nothing changes in Postgres.
- This spec imports no liquidations from Tardis, so liquidation history starts at deploy.

## Follow-up

- [ ] Closes spec 0007's "Feature 20: add its types to `include_types` and the compactor's type list" and spec 0008's "Feature 20 adds funding to own recorder days"; update both on build.
- [ ] Feature 12 (alerts): alert on a `context_feed_down` longer than 5 minutes, on `context_feed_lagging`, and on an hour missing a context file.
- [ ] Nautilus: report upstream that `LiveDataEngine._run_data_queue` (1.231.0) never yields while its queue holds items, which starves every other task on the loop; drop AC-18's reason to exist if a release fixes it.
- [ ] The 2026-10-15 hole (13:58:56 to 15:05:36 UTC) has no gap row, since the rules came later. Optionally add the `silence` and `reconnect` rows by hand, or leave it noted in verify.md step 5.
- [ ] Feature 16 (scalping candidates): the regime gate reads `MarketContext`; stale or missing values block entries as one more `entries_blocked` reason.
- [ ] Feature 10 (funding in P&L): the live funding rate stream is now available on the trader's bus if paper funding is computed rather than read from the account log.
- [ ] If a later Nautilus release exposes open interest or liquidations, replace `ContextFeedActor` with the native subscription and keep the same catalog tables.
- [ ] Deferred extras (not in this spec): Fear & Greed tag, news or event blackout, Deribit options IV and put/call.
