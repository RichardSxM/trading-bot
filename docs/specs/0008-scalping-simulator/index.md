# 0008. Replay recorded BTC order books through the Nautilus matching engine with a pessimistic queue, latency, and fill quality measured

**Date**: 2026-10-08
**Status**: In Progress

## Summary

The scalping simulator replays recorded BTC order book data (the free Tardis first of month days, plus our own recorder's hours) through NautilusTrader's backtest engine. It runs the same strategy code the paper loop runs. Fills are kept honest by four settings: a resting order waits its turn in the queue (and gets no credit when orders ahead of it cancel), every order arrives 25 ms late, large orders walk the book, and maker and taker fees are charged. Each run also measures the two things that decide whether post only entries are worth it: how often a limit order fills, and how far the price moves against you right after it does (adverse selection). It runs on the laptop as a one shot container, and runs independent pieces of work (one variant over one segment) side by side on two worker processes, because the engine replays only about 19,000 events a second. Tardis snapshot blocks in the middle of a day are written as level changes against the book carried so far, so resting orders keep their place in the queue and those hours stay usable. Results go to the laptop's Postgres and to a Markdown plus JSON report, so the promotion gate (feature 15) can score them.

## Requirements

**User stories**:
- As the trader, I want to replay a scalping strategy over many recorded days with realistic fees, queue position, and latency, so I only promote strategies whose edge survives costs.
- As the trader, I want to see how often my limit orders fill and what happens to price after they do, so I can choose between maker and taker entries on evidence.
- As the trader, I want a strategy that is known to lose to show a loss of the expected size, so I can trust the simulator is not flattering results.
- As the trader, I want the replay to make the same decisions the paper loop made on the same hours, so I know the strategy code runs unchanged.

**Acceptance criteria**:
- **AC-1**: `lab import-tardis <from> <to>` (UTC days, both inclusive) downloads `incremental_book_L2`, `trades`, and `derivative_ticker` for exchange `cryptofacilities`, symbol `PF_XBTUSD`, on every first of month day in the range, with the `tardis-dev` client and no API key. Any other day in the range is refused before anything downloads (exit 64, message: free days are the first of each month only). It converts each day hour by hour into the spec 0007 catalog layout under `<LAB_CATALOG_DIR>/tardis/`. Tardis `local_timestamp` becomes `ts_init` and `timestamp` becomes `ts_event`. Rows pass through the same reorder window as the compactor before hour assignment, and price and size precision come from the pinned instrument. Each hour of `order_book_deltas` begins with the carried book as snapshot deltas, built by the same book carry code the compactor uses. It also writes `trade_tick`, one `crypto_perpetual` row per hour from the pinned instrument file, `funding_rate_update` rows (only rows where the rate changes), and `gaps/<day>.parquet` with `reconnect` and `silence` (10 s or more between deltas) gaps. Every Tardis snapshot block after the day's first is written not as a snapshot but as the level changes against the carried book, from the pure `trading_core.lab.book_diff.diff_book`: `DELETE` for a level the block lacks, `UPDATE` for a level whose size differs, `ADD` for a new level, sorted bids then asks by price, each row with the block's `CLEAR` row `ts_event`, `ts_init`, and `sequence`, flags 0 except `F_LAST` on the last row, and nothing at all when the block matches the carried book. So no `CLEAR` row is written after an hour's start. The day's first block is always written as a snapshot. The carry applies every row the import writes (snapshot rows and diff rows alike), so the next hour's start snapshot is the book after the diff. When a block falls at an hour's first `ts_init`, the hour start snapshot rows come first in the file. A `reconnect` gap (from the previous delta's `ts_init` to the block's first row) is written only when that pause is 10 s or more. Silence is measured on the raw CSV rows, blocks included, and a pause that ends in a block gets only the `reconnect` row, not also a `silence` row. A day's `manifests/<day>.json` is written last, as its commit point. The raw CSV files of a day are deleted once its manifest exists. Rerunning a converted day does nothing and logs `lab_import_day_skipped`. A day that was killed partway (no manifest) is converted again from scratch, to the same bytes.
- **AC-2**: Before each day, the import checks that the size of everything under `<LAB_CATALOG_DIR>` (both trees and any `.raw` files) plus that day's expected raw and converted size (`LAB_TARDIS_DAY_GB`, default 3) stays at or under `LAB_CATALOG_CAP_GB` (default 40). If not, it stops with exit 3 and logs ERROR `lab_catalog_full`, keeping the days already converted. `lab prune [--source tardis|own]` deletes that source's tree (both trees with no flag) and prints the bytes freed.
- **AC-3**: `lab replay <runfile> [--from D] [--to D] [--seed N] [--queue-bound] [--sweep-latency] [--pair-entry]` validates the run file and every override at startup. A bad file or value exits 64 with every problem listed, before anything is read or written. It then checks that every required day has a manifest in the run's source tree. For `own`, every UTC day of the range is required. For `tardis`, only the first of month days inside the range are required (the others never exist). Missing days are listed, the command exits 2, and it writes no row, report, or bundle. A range with no required day at all also exits 2.
- **AC-4**: The segment planner (pure, `trading_core.lab.segments`) takes the range's hours, the gap rows (from the gap files of every required day and of the day before the first one, since a gap is filed under its start day), and the `incomplete_hours` of each manifest's `order_book_deltas` and `trade_tick` streams. It drops every hour that overlaps any gap row (any kind, edges included), every incomplete hour, every hour without both an `order_book_deltas` file and a `trade_tick` file, and every hour whose first delta is not part of a snapshot (no `CLEAR` flag on its first row). The clean hours that remain are grouped into segments of consecutive hours, and a segment is split at every 00:00 UTC, so no segment crosses a UTC midnight. Each skipped hour carries one reason (`gap:<kind>`, `incomplete`, `missing`, `no_snapshot`, the first that applies in that order). Each segment replays in its own fresh engine and starts flat. Within a segment, the synthetic snapshot rows at the start of every hour after the first are dropped from the replay stream (the book is already correct there), so resting orders keep their place in the queue across hour boundaries. The rows dropped are exactly the contiguous run from a `CLEAR` row to the next `F_LAST` row at the first `ts_init` of each hour after the segment's first. Any other `CLEAR` or `F_SNAPSHOT` row inside a segment after its first batch makes the hour stream raise the typed `UnexpectedSnapshotError` (the engine would move every resting order to the front of its queue), which fails the variant under AC-13.
- **AC-5**: Each segment's venue is `KRAKEN` with `book_type=L2_MBP`, `oms_type=NETTING`, `account_type=MARGIN`, `MakerTakerFeeModel` (which reads the instrument's fees, so the engine gets a copy of the pinned instrument with `maker_fee` and `taker_fee` set to the run's values, defaults 0.0002 and 0.0005), `LatencyModel(base_latency_nanos=latency_ms × 1,000,000, insert_latency_nanos=0, update_latency_nanos=0, cancel_latency_nanos=0)` at the run's `latency_ms` (default 25; in 1.231.0 the extra values add to the base, and latency applies one way, from strategy to venue), `queue_position=True`, `liquidity_consumption=True`, `trade_execution=True`, `bar_execution=False`, `use_reduce_only=True`, and the run's `starting_balance_usd` and `leverage`. Each segment's `engine.run` gets the segment's `start` and `end` (without `start`, time bars start from the 1970 clock origin). Quote ticks are made from the replayed book: the hour stream generator keeps its own L2 book, and whenever a delta batch changes the top of the book it emits a `QuoteTick` (best bid, best ask, their sizes) at that batch's `ts_init`, right after the batch in the same sorted stream (never inside a snapshot block). So quotes reach the data engine, the cache, and bar aggregation exactly like live quotes.
- **AC-6**: The base variant is named `base`. `--queue-bound` adds `queue=optimistic` (the same run with `queue_position=False`). `--sweep-latency` adds `latency=0` and `latency=4x` (latency 0 and 4 × `latency_ms`). `--pair-entry` adds `entry=taker` (strategy config `entry_mode="taker"`). A strategy without an `entry_mode` field, or a run file that already sets `entry_mode = "taker"`, refuses this flag at startup (exit 64). A variant identical to `base` or to another variant is never run (so `--sweep-latency` with `latency_ms = 0` adds nothing). Variant names use `-` in file names (`latency-4x.md`). With several flags, each flag adds its own variants, changing one thing against `base` (no cross product). Units are started in plan order (variants in this order, `base` first, then segments in order), and bundles and reports are written in the same variant order, whatever order the units finish in. All variants of one command share one `batch_id` (a uuid7). Each variant is its own `strategy_runs` row whose `run_config` carries `batch_id` and `variant`.
- **AC-7**: The replay runs the very `KfsStrategy` subclass the paper loop runs, built from the run file's `[strategy.config]`. It is given a `PauseGate` that is loaded and not paused, the real `EntryBlocks`, and a `record` callable that collects every row it emits (decisions and risk events) into the variant's bundle. A `LabRecorder` actor builds the order, order event, fill, and position rows from the engine's events through the paper loop's pure mappers (`trading_core.recording.mappers`: `order_row`, `order_event_row`, `fill_row`, `position_row`, `decision_id` from the `decision:` tag), with no spool and no Redis. `check_entry` runs unchanged with the run's `RiskLimits`. Each segment's strategy gets the order id tag `S<n>` (its segment number), so order, trade, and position ids never repeat within a run. Nothing in the lab subclasses or patches a strategy.
- **AC-8**: When a segment's data ends, the lab's runner (not the strategy) cancels the strategy's resting orders and closes any open position with a market order, paying the taker fee. It first records a `decisions` row with intent `exit` and reason `segment_end`, and tags the order with it. The stream then gets one final synthetic delta batch (empty, at the last delta's `ts_init` + latency + 1 ms, with the last book unchanged), so the cancel and the close reach the venue and fill against the last book. The segment's `engine.run` `end` is that batch's `ts_init`. If the book cannot fill the whole position, the rest is closed at the last mid with the taker fee, and the run's report and `run_config` carry `segment_end_partial: true`. The reason `segment_end` lives on the decision (positions have no exit reason column).
- **AC-9**: On `tardis` source runs, at each UTC funding boundary (period confirmed in the Milestone 1 gate; Kraken pays hourly), an open position pays or earns `−signed_qty × mark_px × rate`. Here `signed_qty` is the engine's position at that instant (positive for long), `mark_px` is the mid at that instant, and `rate` is the fractional rate of the last `funding_rate_update` at or before it (the field confirmed in the gate). Only boundaries strictly inside a segment count (a segment is flat at both ends). The unit records, at each such boundary, the mid, the signed position, and its `position_id`, and returns them in its `UnitResult`. The parent then has the pure `trading_core.lab.funding` function apply the rates (the gate showed the engine applies no funding itself). A boundary with no `funding_rate_update` at or before it in the run's data raises the typed `FundingRateMissingError`, which fails the variant (AC-13). Each payment is a `funding_payments` row with source `computed`, `rate` set, and `position_id` of the position open at that instant. It counts in `positions.funding_total`, `run_results.funding_total`, and the net P&L, while `positions.realized_pnl` stays before funding, as in paper. `own` source runs set `run_config.funding_source = "none"`, write no funding rows, and their report headline says "before funding".
- **AC-10**: For each completed variant, `run_fill_stats` holds one row per liquidity side (`maker`, `taker`) that had at least one entry order. Only entry orders count (exits, stops, and `segment_end` closes do not). An order belongs to the side of its fills' `liquidity_side`. An order with no fill belongs to the side its type implies: post only and limit orders are `maker`, market orders are `taker`. A post only order the venue rejects counts as placed and not filled. `orders_placed` counts orders on that side, `orders_filled` the ones with any fill, `fill_count` their fills, and `fill_rate` = filled ÷ placed. The markout at horizon h is `side × (mid(fill_ns + h) − fill_px) ÷ fill_px × 10,000`, where `side` is +1 for a buy, and `mid(t)` is the last mid at or before t in the same segment. The mean is weighted by fill size. A fill whose `fill_ns + h` lies after its segment's last delta is left out of that horizon's mean, and the report shows how many fills were left out at each horizon. A horizon with no fill left in it is NULL.
- **AC-11**: Before its first variant, `lab replay` ensures the `instruments` row from the pinned instrument (through `upsert_instrument`; the pin carries Kraken's base tier fees, `maker_fee` 0.0002 and `taker_fee` 0.0005, written in by hand, because the recorded `crypto_perpetual` row has 0) and the strategy version through `ensure_strategy_version`. A completed variant then calls `write_run_bundle` once, connected as `lab_app`, with mode `replay`, env `demo`, status `completed`, its decisions, risk events, orders, order events, fills, positions, funding payments, one `run_results` row (sample `full`), and its `run_fill_stats` rows. It writes `<LAB_REPORTS_DIR>/<batch_id>/<variant>.md`, `<variant>.json`, and `<variant>.bundle.json` (the exact bundle, for feature 15). Then it writes `<batch_id>/index.md`, which lists every variant side by side.
- **AC-12**: Two runs with the same run file, overrides, data, and seed give identical report JSON (except `run_id`, `batch_id`, `started_at`, and `ended_at`), and identical rows apart from ids and those timestamps. This holds with `LAB_WORKERS=1` and `LAB_WORKERS=2` alike: the unit list and the `S<n>` tags come from the plan, the image sets `PYTHONHASHSEED=0`, the parent merges a variant's rows sorted by (segment, `ts`, sequence within the unit), and the parent stamps `run_id` on the merged rows (units return rows without it).
- **AC-13**: A variant fails when an exception escapes `engine.run` or the lab's runner in any of its units, or a worker process dies (a strategy that catches and logs its own error does not fail the variant). Its units not yet started are dropped, and its units still running are terminated. On SIGINT or SIGTERM the parent terminates every running unit, writes no bundle for any variant that is not complete, writes their reports and `index.md` rows with `status: interrupted`, and exits 130. `index.md` is always written, and it marks failed and interrupted variants (it still lists the others when `base` failed). A failed variant writes no database row (the `instruments` and `strategy_versions` rows of AC-11 are idempotent, hold no results, and may stay). Its JSON and Markdown report carry `status: failed` with the exception's type and message, and it logs ERROR `lab_variant_failed`. The other variants of the batch still run. The command exits 1 if any variant failed, else 0.
- **AC-14** (the loser check): `random_taker` with `apps/lab/runs/random_taker.toml` over one Tardis day loses money in total. Its mean net P&L per closed trade lies within `expected ± max(3 × standard error, 0.5 bps of mean notional)`, where expected is `−(taker_fee × (entry notional + exit notional) + qty × (entry spread + exit spread) ÷ 2)` in USD, averaged over trades. Each spread is the quoted spread (best ask − best bid, in USD) on the book at the moment the order was submitted (not when it filled), and the standard error comes from the per trade net P&L values. Trades that cross a funding boundary or close at `segment_end` are left out of the band (and still count in the total). The report prints the expected value, the band, the trades used, and pass or fail. Unit tests check the band math; the full check runs on real data (verify.md).
- **AC-15** (the parity check): `placeholder_timed`, replayed with the paper loop's config over every clean `own` hour that lies wholly inside a paper run window and outside any pause, makes decisions matching the paper ones. A pair matches when both have intent `enter`, the same side, and `decided_ns` within 1 s of each other, whether approved or rejected. Matches ÷ max(paper decisions, replay decisions) is at least 0.95. Only boundaries at least `lookback_min + hold_min` minutes after a segment start count (paper may hold a position the replay does not), and boundaries inside a `stale_feed` or `stop_not_accepted` block window are left out (paper was blocked there for reasons the replay cannot see). Paper decisions, run windows, pause windows, block windows, and each paper run's config hash come from the CSV written by `scripts/fetch-paper-decisions.sh`. Hours of a paper run whose config hash differs from the run file's are left out. `lab parity <runfile> <csv>` prints the ratio and every unmatched decision, exits 1 under 0.95, and exits 2 when no decision is left to compare.
- **AC-16**: Work runs as units (one variant over one segment). The parent schedules them itself: one spawned `multiprocessing.Process` per unit, at most `LAB_WORKERS` (default 2) at a time, each sending its `UnitResult` back over a pipe. A killed child fails only its own variant. With the `lab` container under `mem_limit: 3g` there is no OOM kill, and each unit's `ru_maxrss` stays under 1 GB. `lab_hour_replayed` logs the current RSS each hour, and `lab_unit_done` logs `ru_maxrss`; the lab does not enforce the 1 GB itself (the container limit does), and verify.md checks it. A Tardis day is usually one segment, so a single variant gains nothing from the workers; only variants and days run side by side. One full clean Tardis day of `random_taker` (one variant) replays in under 75 minutes, and one full clean own day in under 135 minutes. A `--pair-entry` batch of `maker_probe` over one Tardis day (two variants) finishes in under 85 minutes wall clock with 2 workers. Each segment's engine is fed through `BacktestEngine.add_data_iterator` by a generator that yields one hour at a time, so at no point is more than the current hour's data in memory. The mid series for markouts is a rolling buffer of the last 30 s plus the pending fills' horizons.
- **AC-17**: The lab exits 78 at startup with `TRADING_ENV=live` (the env guard). It imports no Kraken execution client factory and mounts no Kraken key secret. It connects to Postgres only as `lab_app`, which has SELECT and INSERT on the bundle tables and nothing else. On a host without `postgres_lab_password` (the VPS), the migration creates `lab_app` as `NOLOGIN`, so the role exists but cannot connect.
- **AC-18**: A `--pair-entry` batch (for example `maker_probe` over one Tardis day) has an `index.md` that shows `base` (post only entries) and `entry=taker` side by side: net P&L, fees, trade count, fill rate, and maker and taker markouts at 1 s, 5 s, and 30 s.

## Decision

**Chosen option**: Option 2: Nautilus `BacktestEngine` per clean segment, with built in queue, latency, and fee models, plus our own fill quality measurement

Each clean segment of recorded hours replays through a fresh NautilusTrader `BacktestEngine`, with the venue's built in queue position, liquidity consumption, latency, and maker taker fees switched on. The same strategy classes as paper run inside it, and pure core code measures fill rate, markouts, funding, and the loser band from the replay's own events.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

### Pipeline

```
Tardis free day ──lab import-tardis──► catalog-lab/tardis/ ─┐
B2 catalog ──fetch-catalog.sh --into catalog-lab/own──► catalog-lab/own/ ─┤
                                                             ▼
run file (TOML) ──► lab replay ──► segment planner ──► units (variant × segment)
                                                             ▼
                          worker pool (LAB_WORKERS, a fresh spawned process per unit):
                                                     hour stream generator
                                                     (deltas + trades + synthesized quotes,
                                                      one hour per chunk) ──► BacktestEngine
                                                     (L2 book, queue, latency, fees,
                                                      same KfsStrategy + check_entry,
                                                      LabRecorder actor) ──► UnitResult
                                                             ▼
                                   parent: merge units in segment order, fill stats, funding, metrics
                                                             ▼
                                   per variant: bundle ──► laptop Postgres (lab_app)
                                                └────────► reports/<batch>/<variant>.md|.json|.bundle.json
VPS Postgres ──fetch-paper-decisions.sh──► paper.csv ──► lab parity
```

### Data model sketch

**New table `run_fill_stats`** (one row per replay run and liquidity side):

| Column | Type | Null | Rule |
|---|---|---|---|
| `run_id` | uuid | no | PK part; composite FK `(run_id, env)` → `strategy_runs (id, env)` |
| `env` | text | no | `CHECK (env IN ('demo','live'))` |
| `liquidity` | text | no | PK part; `CHECK (liquidity IN ('maker','taker'))` |
| `orders_placed` | integer | no | `CHECK (orders_placed >= 0)` |
| `orders_filled` | integer | no | `CHECK (orders_filled BETWEEN 0 AND orders_placed)` |
| `fill_rate` | numeric | no | `orders_filled / orders_placed` (a row exists only when `orders_placed ≥ 1`); `CHECK (fill_rate BETWEEN 0 AND 1)` and `CHECK (orders_placed >= 1)` |
| `fill_count` | integer | no | `CHECK (fill_count >= 0)` |
| `markout_1s_bps` | numeric | yes | AC-10 |
| `markout_5s_bps` | numeric | yes | AC-10 |
| `markout_30s_bps` | numeric | yes | AC-10 |
| `computed_at` | timestamptz | no | UTC |

PK `(run_id, liquidity)`. The composite FK index covers `run_id`. No UPDATE path: rows are written once inside the bundle.

**Reused from spec 0002, unchanged**: `strategies` (kind `scalping`), `strategy_versions`, `instruments`, `strategy_runs` (mode `replay`, env `demo`, `trader_id` `BACKTESTER-LAB`), `run_results` (sample `full`), `decisions`, `orders`, `order_events`, `fills`, `positions`, `funding_payments` (source `computed`).

**`strategy_runs.run_config` for a replay** (jsonb): `batch_id`, `variant`, `run_file` (path), `run_file_sha256`, `overrides`, `seed`, `source` (`tardis` | `own`), `instrument_id`, `maker_fee`, `taker_fee`, `latency_ms` (the variant's), `queue_model` (`pessimistic` | `optimistic`), `entry_mode` (if any), `starting_balance_usd`, `leverage`, `risk_limits`, `funding_source` (`tardis` | `none`), `segments` (list of `[start_ns, end_ns]`), `skipped_hours` (list of `[hour_ns, reason]`), `segment_end_partial`, `nautilus_version`, `git_commit`.

**Run row fields**: `id` is a uuid4 the runner makes before the engine starts. `started_ns` and `ended_ns` are wall clock (UTC) around the variant (excluded from AC-12). `data_start_at` and `data_end_at` are the first segment's start and the last segment's end. `trader_id` is `BACKTESTER-LAB` for every segment.

**Result fields** (`run_results`, sample `full`, currency `USD`, from pure `trading_core.lab.metrics`): `return_after_costs` = net P&L (realized after fees, plus funding) ÷ `starting_balance_usd`; `max_drawdown` = the largest fall of realized equity from its running peak, as a fraction of that peak, with equity stepping after each closed trade and funding payment (funding before a close at the same nanosecond), starting from `starting_balance_usd` as the first peak; `win_rate` = closed trades with net P&L (after fees and funding) above zero ÷ closed trades; `trade_count` = closed positions; `fees_total` and `funding_total` = sums over the run; `risk_adjusted_ratio` and `ratio_kind` NULL (feature 15 owns them). `computed_at` here and in `run_fill_stats` is the wall clock when the bundle is built.

**Code changes to spec 0002's repository**: `RunBundle` gains `fill_stats: Sequence[RunFillStatsRow]` and `risk_events: Sequence[RiskEventRow]`, and `write_run_bundle` inserts them (fill stats after `run_results`, risk events after decisions and orders, since they reference both), in the same transaction. `RunFillStatsRow` is a new frozen dataclass in `trading_core.recording.rows`, with `Decimal` numbers.

**Migration** `20261008_0005_lab.py` (additive): create `run_fill_stats`; create role `lab_app` through `db_roles` (`LAB_ROLE`, secret `postgres_lab_password`, an optional secret: when its file is missing, `db_roles` creates or alters the role `NOLOGIN` with no password, and the `migrate` service's `secrets:` lists it as optional on the laptop only); `GRANT SELECT, INSERT` on `strategies`, `strategy_versions`, `instruments`, `strategy_runs`, `run_results`, `run_fill_stats`, `decisions`, `orders`, `order_events`, `fills`, `positions`, `funding_payments`, `risk_events` to `lab_app`; `GRANT SELECT` on `run_fill_stats` to `trader_app`, `dashboard_app`, and `backup_app` (the existing default privileges also cover it). Downgrade drops the table and the role's grants. The role itself stays, like the other app roles.

### Catalog on the laptop

| Path | Written by | Notes |
|---|---|---|
| `catalog-lab/tardis/data/{order_book_deltas,trade_tick,crypto_perpetual,funding_rate_update}/PF_XBTUSD.KRAKEN/<interval>.parquet` | `lab import-tardis` | spec 0007 layout and file naming (`class_to_filename`, `urisafe_identifier`, `_timestamps_to_filename`) |
| `catalog-lab/tardis/gaps/<day>.parquet`, `manifests/<day>.json` | `lab import-tardis` | the same schemas as spec 0007; the manifest is the day's commit point |
| `catalog-lab/tardis/.raw/<day>/` | `lab import-tardis` | the downloaded CSV, deleted after the day commits |
| `catalog-lab/own/...` | `scripts/fetch-catalog.sh --into catalog-lab/own <from> <to>` | the VPS catalog's days, unchanged |
| `lab-reports/<batch_id>/` | `lab replay` | reports and bundle files |
| `lab-reports/paper/<from>_<to>.csv` | `scripts/fetch-paper-decisions.sh` | paper decisions, run windows with config hash, pause windows, block windows |

`catalog-lab/` and `lab-reports/` are added to `.gitignore`. Tardis days and own days live in separate trees, because a first of month day can exist in both.

**Pinned instrument**: `apps/lab/src/lab/instruments/PF_XBTUSD.KRAKEN.json` holds the `CryptoPerpetual` definition, copied once from the newest `crypto_perpetual` row of the own catalog, with `maker_fee` 0.0002 and `taker_fee` 0.0005 written in by hand (the recorded row has 0, because Kraken publishes zero fees on the instrument; a `_note` key in the file says so and the loader ignores it). The import refuses (exit 1, `lab_tick_mismatch`) a Tardis day whose prices are not multiples of its `price_increment`, or whose sizes are not multiples of its `size_increment`, rather than rounding.

### Run file (TOML, `apps/lab/runs/*.toml`)

```toml
source = "tardis"                    # "tardis" | "own"
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2026-09-01                    # UTC day, inclusive
to = 2026-09-01                      # UTC day, inclusive
seed = 7
maker_fee = "0.0002"                 # strings, parsed as Decimal
taker_fee = "0.0005"
latency_ms = 25
starting_balance_usd = "10000"
leverage = "2"

[risk]                               # RiskLimits (spec 0006 check_entry), same field names
max_order_notional = "2000"
max_leverage = "2"

[strategy]
name = "random_taker"                # a key of the lab strategy registry
[strategy.config]                    # fields of that strategy's StrategyConfig
interval_s = 60
hold_s = 30
notional_usd = "1000"                # quantity = notional ÷ mid, floored to size_increment
```

It is validated by a pydantic model in `lab.runfile` (extra keys forbidden, `from` ≤ `to`, fees in [0, 1), latency ≥ 0, `[risk]` keys exactly the `RiskLimits` fields). `[strategy.config]` is validated by building the strategy's own `StrategyConfig`. CLI overrides replace the matching keys before validation. `run_file_sha256` hashes the file bytes, and `overrides` records what changed.

**Strategy registry** (`lab.strategies`): `random_taker` → `RandomTaker` and `maker_probe` → `MakerProbe` (both new, in `trading_core/node/random_taker.py` and `trading_core/node/maker_probe.py`, sharing the pure timing and side rule in `trading_core/strategy/random_taker.py`); `placeholder_timed` → `PlaceholderTimed`. Each entry names the class, its config class, and its factory, using the same constructor arguments the trader passes. With `LAB_TEST_STRATEGIES=1` only (tests), the registry also holds `raise_probe`, a `random_taker` that raises out of `on_quote_tick` at a configured hour, for AC-13.

**`random_taker`**: every `interval_s` seconds (on UTC multiples, from the segment start), while flat, it draws a side from `random.Random(seed ^ boundary_ns)` (so a draw does not depend on earlier draws) and submits a market entry of `notional_usd ÷ mid`, floored to the size increment. It exits with a market order `hold_s` seconds after the entry fills. It places no stop, has no `entry_mode` field, and refuses nothing beyond `check_entry`.

**`maker_probe`**: the same timing and side draw as `random_taker`, but with `entry_mode` (`"maker"` default, or `"taker"`). In maker mode it posts a post only limit at the best bid (buy) or best ask (sell) of the last quote, and cancels it if unfilled after `entry_timeout_s` (default 10). In taker mode it enters at market. It always exits at market `hold_s` seconds after the fill. It has no edge by design. It exists so the maker effects (AC-10, AC-18) can be checked before feature 16 brings real candidates, and its run file is `apps/lab/runs/maker_probe.toml`.

### API surface (CLI, `docker compose run --rm lab <command>`)

| Command | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|
| `lab import-tardis <from> <to>` | days (req) | `catalog-lab/tardis/` days; log per day | none (Tardis free days need no key) | 64 non first of month day; 3 catalog cap; 1 download or tick mismatch |
| `lab replay <runfile> [overrides] [variant flags]` | run file (req); `--from`, `--to`, `--seed`, `--queue-bound`, `--sweep-latency`, `--pair-entry` (opt) | rows via `write_run_bundle`; `lab-reports/<batch_id>/` | `lab_app` Postgres role | 64 bad input; 2 missing days; 1 a variant failed; 130 interrupted; 78 live env |
| `lab parity <runfile> <csv>` | run file (req), paper CSV (req) | ratio, unmatched list | none (no DB) | 1 under 0.95; 2 missing days |
| `lab prune [--source tardis\|own]` | source (opt) | bytes freed | none | none |
| `scripts/fetch-paper-decisions.sh <from> <to>` (host) | days (req) | `lab-reports/paper/<from>_<to>.csv` | SSH to `kfs-vps`, then `psql` as `dashboard_app` (read only) | 64 bad args; 1 SSH or query failure |

Only `replay` connects to Postgres, so `import-tardis`, `parity`, and `prune` run with `docker compose run --rm --no-deps lab ...`. `parity` replays in memory and writes nothing to the database.

**Reports**: a pydantic model `LabReport` (status, error, run and batch ids, variant, `run_config`, segments, skipped hours, the `run_results` numbers, the fill stats, the loser band when the strategy is `random_taker`, and the "before funding" flag) dumped with `model_dump(mode="json")`, Decimals as strings, nanosecond times as integers, keys sorted. The Markdown report renders the same model. `.bundle.json` is the `RunBundle` in the same encoding.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| import | free days | first of month days in `<from>`..`<to>`; others refused (AC-1) |
| import | book, trades, funding rows | `TardisCSVDataLoader.stream_deltas`, `stream_trades`, `stream_funding_rates` on the downloaded CSV (`instrument_id` PF_XBTUSD.KRAKEN) |
| import | hour start snapshot | the compactor's `BookCarry`, moved to `trading_core.catalog` (see Code placement) |
| import | mid day snapshot block | written as `diff_book(carried book, block)` level changes, never as a `CLEAR` (AC-1) |
| import | `reconnect` gap | a Tardis snapshot block (`is_snapshot` rows) after the day's first whose pause from the previous delta's `ts_init` is 10 s or more, spanning that pause |
| import | instrument row | the pinned JSON |
| import | expected day size | `LAB_TARDIS_DAY_GB` |
| replay | range | run file `from`/`to`, else overrides |
| replay | clean hours, skip reasons | `gaps/<day>.parquet`, `manifests/<day>.json` (`incomplete_hours`), the presence of `data/order_book_deltas/...`, and each hour's first row flags (AC-4) |
| replay | units | the segment planner's segments (split at UTC midnight) times the variants, in plan order (AC-4, AC-16) |
| replay | worker count | `LAB_WORKERS` (AC-16) |
| replay | fill stats of a variant | the per fill markouts and per order sides from all its `UnitResult`s, merged in segment order, then AC-10's math (never an average of unit means) |
| replay | fees, latency, balance, leverage, risk limits | run file, then the variant (AC-5, AC-6) |
| replay | quotes and bars | the hour stream generator's own book, emitted into the data stream (AC-5); bars `…-1-MINUTE-MID-INTERNAL` aggregate those quotes, as in paper |
| replay | per segment strategy id tag | `S<n>`, the segment's index in the plan (AC-7) |
| replay | orders, order events, fills, positions | the `LabRecorder` actor over the engine's events, through `trading_core.recording.mappers` (AC-7) |
| replay | instrument for the engine | the pinned JSON with `maker_fee` and `taker_fee` replaced by the run's (AC-5); the `instruments` row keeps the pinned values (AC-11) |
| replay | random side | `random.Random(seed ^ boundary_ns)` |
| replay | segment end close price | the segment's last book (AC-8) |
| replay | funding payment | `funding_rate_update` rows, the mid at funding time, the signed position (AC-9) |
| replay | maker or taker for an entry order | its fills' `liquidity_side`; with no fill, its type (AC-10) |
| replay | entry or exit | the order's decision intent (`enter`) through its `decision:` tag |
| replay | mid at t | the `LabRecorder`'s rolling `(ts, mid)` buffer from the quote ticks it receives (AC-16) |
| replay | spread at submit (loser band) | the last quote tick at or before the order's `ts_init`, stored on the order's record in the `LabRecorder` (AC-14) |
| replay | quantity | `notional_usd ÷ mid`, floored to `size_increment` |
| replay | `return_after_costs`, `max_drawdown`, `win_rate`, `trade_count`, `fees_total`, `funding_total` | pure `trading_core.lab.metrics` on the variant's closed positions and fills, in USD `Decimal`; `max_drawdown` on the realized equity after each closed trade; `risk_adjusted_ratio` and `ratio_kind` NULL (feature 15 owns them) |
| replay | strategy version | `ensure_strategy_version(name, "scalping", git_commit, config)`; `git_commit` from the image's `GIT_COMMIT` build arg |
| replay | `trader_id` | `BACKTESTER-LAB` |
| parity | paper decisions, run windows, pauses, blocks | the CSV, one `kind` column per row type: `decision` (`decided_ns`, `intent`, `side`, `risk_verdict`, `run_id`) for `paper` runs; `run` (`started_at`, `ended_at`, and the sha256 of the canonical `strategy_versions.config`); `pause` (`control_commands` `pause`/`unpause` with `outcome = 'executed'`); `block` (`risk_events` of kind `stale_feed`, and the `stop_not_accepted` events, with start and end from their `details`) |
| parity | paper strategy config | the run file `apps/lab/runs/placeholder_paper.toml`, copied from the VPS trader's settings (`PLACEHOLDER_*`) |

### Code placement

- `packages/core/src/trading_core/lab/` (pure, no I/O, no Nautilus): `segments.py` (AC-4, with the midnight split), `book_diff.py` (AC-1, `diff_book` over `(side, price, size)` with `Decimal`), `markouts.py` and `fill_stats.py` (AC-10), `funding.py` (AC-9), `metrics.py`, `loser_band.py` (AC-14), `parity.py` (AC-15 matching), `variants.py` (AC-6 expansion).
- `packages/core/src/trading_core/catalog/` (Nautilus and pyarrow adapter, shared by recorder and lab): `BookCarry`, `HourSink`, the interval naming, and gap and manifest writing, moved out of `apps/recorder/src/recorder/compactor/` with no change in behavior. The compactor imports them from here.
- `packages/core/src/trading_core/strategy/random_taker.py` (pure side draw and timing) and `packages/core/src/trading_core/node/random_taker.py` (thin `KfsStrategy`).
- `apps/lab/` (new workspace app, package `lab`): `main.py` (CLI, argparse), `runfile.py`, `settings.py` (`LabSettings(Settings)`), `tardis_import.py`, `stream.py` (the hour stream generator: reads an hour, drops inner snapshots, refuses any other inner `CLEAR`, synthesizes quotes, adds the final segment end batch), `units.py` (`run_unit(plan, unit) -> UnitResult`: engine setup for one segment of one variant, segment end close; `UnitResult` is a picklable frozen dataclass with the unit's rows, per fill markouts, order sides, the mid, signed position, and `position_id` at each funding boundary inside the segment, the partial close flag, wall seconds, and `ru_maxrss`), `replay.py` (variants, the unit list, the unit scheduler (spawned processes, pipes, termination of a failed variant's units, signal handling), merging units in segment order, funding, metrics, bundle assembly), `actors.py` (`LabRecorder`), `report.py`, `strategies.py`, `instruments/`, `runs/`, `tests/`.
- `scripts/fetch-paper-decisions.sh` (host).

### Compose

The new service `lab` uses `profiles: ["lab"]` (never started by `up`), the image `kfs/lab` built like the trader's (it carries Nautilus and `tardis-dev`), and `mem_limit: 3g` (the laptop's Docker VM has 4 GB; two workers at under 1 GB each plus the parent leave room for Postgres). The image sets `PYTHONHASHSEED=0` (AC-12). It mounts `./catalog-lab:/catalog-lab` and `./lab-reports:/lab-reports` (read and write), plus the secrets `postgres_lab_password` only. It has `depends_on: postgres (healthy), migrate (completed)` (the other commands pass `--no-deps`), and `TRADING_ENV=demo`. `LabSettings` extends the base `Settings` and needs no Redis setting. `scripts/dev-secrets.sh` creates `postgres_lab_password`. The VPS Compose override does not include `lab` (it never runs there).

### Key invariants

- No `CLEAR` reaches an engine after a segment's first batch, so the engine never moves resting orders to the front of their queues (AC-1, AC-4).
- Results do not depend on the worker count: units are fixed by the plan and merged in segment order (AC-12).
- No fill ever happens against an hour that has a gap, a hole, or late rows, or that lacks a starting snapshot (AC-4).
- Every segment starts flat and ends flat (AC-4, AC-8).
- A replay writes only through `write_run_bundle`: all of a variant's rows or none (AC-11, AC-13).
- Money, prices, sizes, fees, funding, markouts, and ratios are `Decimal` or Nautilus types; no float reaches a row or a report number.
- The lab never has a Kraken execution client, a Kraken key, or `TRADING_ENV=live` (AC-17).
- The same inputs give the same outputs (AC-12): no wall clock in replay logic, and every random draw comes from the seed.
- At most one hour of market data in memory per engine (AC-16).

### Security model

There is no new external credential: Tardis free days need no key. `lab_app` has its own secret file, `postgres_lab_password`, read through `trading_core.secrets`, and holds only SELECT and INSERT on the bundle tables. So a lab bug cannot change or delete a paper or live row, and it cannot touch the control or snapshot tables. The paper export runs over the existing SSH access and uses the read only `dashboard_app` role on the VPS. The CSV holds decisions only: no secret and no personal data. The lab runs only on the laptop, and only in demo.

### Configuration required

- `LAB_CATALOG_DIR` [`/catalog-lab`]: root of both source trees.
- `LAB_REPORTS_DIR` [`/lab-reports`]: reports, bundle files, paper CSV.
- `LAB_CATALOG_CAP_GB` [40]: the cap for `catalog-lab`.
- `LAB_WORKERS` [2]: worker processes for replay units, validated 1 to 4 (the laptop has 2 physical cores and a 3g container).
- `LAB_TARDIS_DAY_GB` [3]: the expected size of one converted day, used by the cap check (refined by the gate's measurement).
- Secret file `postgres_lab_password` (laptop only, from `scripts/dev-secrets.sh`).
- Run file keys: see *Run file*. The lab reads no other environment for simulation settings.

### Logs

`lab_import_day_done` (day, rows, bytes, gaps, snapshot_diffs), `lab_import_day_skipped`, `lab_catalog_full` (ERROR), `lab_tick_mismatch` (ERROR), `lab_segment_planned` (segments, units, skipped hours by reason), `lab_hour_replayed` (hour, events, wall seconds, RSS), `lab_unit_done` (variant, segment, wall seconds, `ru_maxrss`), `lab_variant_done` (variant, run_id, net P&L, trades), `lab_variant_failed` (ERROR, variant, error), `lab_batch_done` (batch_id, variants, failed). Every line logged inside a unit carries `variant` and `segment`; lines from different workers interleave, so readers sort by those keys, not by line order.

### Critical test scenarios

**Fixtures**: committed trimmed slices, a few MB each: 5 minutes of real own recorder deltas and trades that start at an hour's snapshot, two of them back to back across an hour boundary, plus a short Tardis CSV sample. Whole hours are too big for git; the full hour and day checks run on real data in verify.md.

- Happy path: the fixture slices replay `maker_probe` with all three variant flags, giving five variants with one bundle each in Postgres and a batch `index.md`. Verifies **AC-3**, **AC-5**, **AC-6**, **AC-11**, **AC-18**
- Loser check: the fixture hours give a total loss inside the band; a test that sets `taker_fee = 0` and asserts the band moves by exactly the fee term. Verifies **AC-14**
- Queue: a post only bid at the best price with 5 BTC ahead fills only after 5 BTC trade at that price; with `queue=optimistic` it fills on the first trade. Verifies **AC-5**, **AC-6**
- Latency: an order submitted at t reaches the book at t + 25 ms and fills at the book of that moment, not of t. Verifies **AC-5**
- Segments: a gap row, an incomplete hour, a missing file, and an hour with no snapshot are each skipped with the right reason; the hours around them become two segments; a position open at the first segment's end closes with `segment_end`. Verifies **AC-4**, **AC-8**
- Markouts and fill rate: a hand built fill series gives the expected bps; a fill 10 s before segment end is in the 1 s and 5 s means but not the 30 s one. Verifies **AC-10**
- Funding: a long across one funding time with rate 0.0001 pays `qty × mid × 0.0001`; an own source run writes none and says "before funding". Verifies **AC-9**
- Failure: a strategy that raises in hour 2 of one variant leaves no rows for that variant, a `failed` report, the other variants complete, exit 1. Verifies **AC-13**
- Missing data: a range with one day lacking a manifest exits 2, with no row and no report. Verifies **AC-3**
- Determinism: the same run twice gives equal report JSON after removing the four allowed fields. Verifies **AC-12**
- Hour boundary: a post only order resting across the two back to back slices keeps its queue place (no snapshot reapplied), and ids from two segments never collide in one bundle. Verifies **AC-4**, **AC-7**
- Snapshot diff: a block that changes, removes, and adds levels against the carried book gives exactly those `UPDATE`, `DELETE`, and `ADD` rows with the block's timestamps and `F_LAST` last; a block equal to the carried book gives no rows; a block after a 12 s pause also gives a `reconnect` gap, one after 80 ms does not. Verifies **AC-1**
- Inner CLEAR: a segment stream with a `CLEAR` row in its second hour (not the hour start) raises `UnexpectedSnapshotError` and fails the variant. Verifies **AC-4**, **AC-13**
- Midnight split: clean hours 22:00 to 02:00 become two segments, split at 00:00 UTC. Verifies **AC-4**
- Worker count: the fixture batch with `LAB_WORKERS=1` and `LAB_WORKERS=2` gives equal report JSON after removing the four allowed fields. Verifies **AC-12**, **AC-16**
- Import: the Tardis CSV sample converts to hours that start with snapshots; a second snapshot block becomes level changes with no `CLEAR`; a rerun is skipped; a non first of month day exits 64; a cap breach exits 3. Verifies **AC-1**, **AC-2**
- Auth/permission: `lab_app` gets `permission denied` on UPDATE of `orders` and on INSERT into `control_commands`; the lab with `TRADING_ENV=live` exits 78. Verifies **AC-17**
- Parity: a CSV built from a replay's own decisions scores 1.0; removing 10 % of rows scores under 0.95 and exits 1. Verifies **AC-15**

## Build plan

Tracer Bullet: Milestone 2 pushes one real hour through every layer (run file, engine, strategy, bundle, Postgres, report) before any layer is thickened.

**Milestone 1: Gate** (throwaway spike in `scripts/spikes/`, findings into rationale.md). Ran 2026-10-08 and did not pass (speed, Tardis snapshots, memory); the spec was updated the same day, see rationale.md "Gate decisions".
1. Replay one own recorder hour of `PF_XBTUSD` deltas and trades through `BacktestEngine` with AC-5's venue settings. Record events per second, wall time, and peak RSS, and extrapolate to a day against AC-16. Satisfies **AC-5**, **AC-16**
2. Prove `queue_position=True` waits for the size ahead, and that a 25 ms `base_latency_nanos` (extras 0) delays the order by exactly 25 ms, on synthetic data and on the real hour. Also record whether a delta that shrinks the level trims the queue ahead (a cancel ahead would then count), and whether a trade plus the delta that reflects the same print consume liquidity twice. Write each answer into rationale.md; if the queue model differs from what AC-5 and the Consequences assume, stop and route back to `/architect`. Satisfies **AC-5**
3. Prove commission rounding: the fee on a 1,000 USD taker fill equals `taker_fee × notional`, rounded to cents, on the engine's instrument copy with the run's fees. Prove quotes emitted in the stream reach `subscribe_quote_ticks` and the `-INTERNAL` bars. Satisfies **AC-5**, **AC-14**
4. Load one Tardis hour with `TardisCSVDataLoader`. Confirm the instrument id mapping, the size unit (BTC), `is_snapshot` handling, whether `local_timestamp` goes backwards, which funding field is the fractional rate, the funding period, and whether the engine applies `FundingRateUpdate` to positions by itself (if it does, AC-9 uses it, and core only reads the result). Satisfies **AC-1**, **AC-9**

**Milestone 2: Thin thread (one hour, end to end)**
5. Scaffold `apps/lab` (workspace member, Dockerfile, Compose service with profile `lab` and `mem_limit: 3g`, `LabSettings` with `LAB_WORKERS`, env guard, `.gitignore` entries, the pinned instrument with its fees written in). Satisfies **AC-11**, **AC-16**, **AC-17**
6. Migration `0005`: `run_fill_stats`, the `lab_app` role (`NOLOGIN` without its secret) and grants, `postgres_lab_password` in `dev-secrets.sh`; extend `RunBundle` and `write_run_bundle` with fill stats and risk events. Satisfies **AC-11**, **AC-17**
7. Run file model, CLI `replay`, the required day check, and the pure segment planner with the midnight split. Satisfies **AC-3**, **AC-4**
8. The hour stream generator (inner snapshots dropped, any other inner `CLEAR` refused, quotes synthesized), `run_unit` and `UnitResult` called in process for now, engine setup per segment through `add_data_iterator` with `engine.run(start, end)`, the `LabRecorder` actor, the strategy registry, `RandomTaker` (pure rule plus thin strategy), segment id tags, and the injected gate, blocks, and record. Satisfies **AC-4**, **AC-5**, **AC-7**, **AC-16**
9. Instrument and version rows, bundle assembly, metrics, `LabReport` Markdown and JSON, and the loser band math, on the fixture slices. Satisfies **AC-11**, **AC-14**

**Milestone 3: Tardis data and funding**
10. Move `BookCarry`, `HourSink`, interval naming, and gap and manifest writing into `trading_core.catalog`; the recorder's tests pass unchanged. Satisfies **AC-1**
11. `lab import-tardis`: download, hour by hour conversion, mid day snapshot blocks as `diff_book` level changes, `reconnect` gaps only after a pause of 10 s or more, `silence` gaps, manifest as commit, CSV cleanup, skip on rerun, cap check, and `lab prune`. Satisfies **AC-1**, **AC-2**
12. Funding payments from `funding_rate_update`, and the "before funding" label for own runs. Satisfies **AC-9**
13. The full loser check on one Tardis day. Satisfies **AC-14**

**Milestone 4: Fill realism and variants**
14. `MakerProbe` (post only entry with timeout, or taker entry), variant expansion, and the `queue=optimistic`, `latency=0`, `latency=4x`, and `entry=taker` variants, sharing a batch id. Satisfies **AC-6**, **AC-18**
15. Segment end cancel and close by the runner, the final synthetic batch, the partial close rule, tagged `segment_end`. Satisfies **AC-8**
16. Fill rate and markouts into `run_fill_stats`, plus the batch `index.md` side by side. Satisfies **AC-10**, **AC-18**

**Milestone 5: Hardening and parity**
17. Determinism test (also across `LAB_WORKERS` 1 and 2), and failure isolation per variant with exit codes, using `raise_probe` behind `LAB_TEST_STRATEGIES=1`. Satisfies **AC-12**, **AC-13**
18. Speed and memory: the unit scheduler (one spawned `multiprocessing.Process` per unit, at most `LAB_WORKERS`, results over pipes), terminating a failed variant's units, SIGINT and SIGTERM handling with exit 130, RSS logging, Python stream speedups (bulk `from_pyo3_list`, row group batches), and the AC-16 timings on the laptop. The 19,000 events a second was measured on one engine. If two engines side by side miss AC-16's bounds, stop and route back to `/architect` rather than loosening them in the build. Satisfies **AC-6**, **AC-13**, **AC-16**
19. `scripts/fetch-paper-decisions.sh`, `apps/lab/runs/placeholder_paper.toml`, `lab parity`, and the parity check on real paper hours. Satisfies **AC-15**
20. Permission tests for `lab_app`, the live env refusal, and `verify.md` complete. Satisfies **AC-17**

## Consequences

**Positive**:
- Feature 15 gets typed, SQL queryable replay results, including fill quality, beside the paper runs, so it can score maker against taker entries on evidence.
- The queue and latency models come from the pinned engine, so there is very little matching code of our own to get wrong.
- Strategy code is shared with paper (AC-7, AC-15), so a strategy that passes replay is the one that trades.
- The loser band (AC-14) is a standing self check: a change that makes the simulator flatter results fails it.

**Negative / tradeoffs**:
- The pessimistic queue ignores cancels ahead of you, so post only strategies will look somewhat worse than reality. The `queue=optimistic` bound shows how much, and the true value lies between the two.
- 25 ms latency is a guess until the live canary (feature 19) measures it.
- Free Tardis days are one day a month, so about 55 BTC days since 2022. That is enough regimes to reject strategies, but thin for fine tuning parameters, and an overfitting risk feature 15 must guard against.
- Results live in the laptop Postgres, so the VPS dashboard cannot see them until feature 15 moves them.
- Moving `BookCarry` and `HourSink` into core touches a deployed service (the compactor). Its tests must pass unchanged before the lab uses them.
- The engine replays about 19,000 events a second: a Tardis day takes about an hour and an own day nearly two per variant. Two workers roughly halve a batch's wall time, but 55 Tardis days with five variants is still several nights, and the laptop is busy while a batch runs.
- Own segments are split at each UTC midnight, adding one forced flat close per own day, and a few more fills whose markout horizon runs past a segment end (the report counts them).
- AC-16's bounds assume two engines scale on the laptop's 2 physical cores. Only one engine was measured, so task 18 confirms it.
- When a diff (like a normal feed `DELETE`) removes the level an order rests at, the engine moves that order to the front of its queue. That is a small optimistic edge inside an otherwise pessimistic queue.
- The Tardis catalog is no longer the raw feed: mid day snapshot blocks become level changes. The book is identical, but the `CLEAR` rows are gone, so the catalog cannot show where Tardis resnapshotted (the import log counts them).
- Own recorder days carry no funding until feature 20.

**Neutral**:
- A fourth workspace app, `apps/lab`, and a new image `kfs/lab`; feature 13's candle lab can join it.
- One additive migration and one new Postgres role.
- One new dependency, `tardis-dev`, in the `lab` app only.

## Follow-up

- [ ] Feature 15 decides how laptop replay results reach the VPS dashboard (load the `.bundle.json` files, or another path).
- [ ] Feature 19's live canary measures real insert and cancel latency, and should replace the 25 ms default in the run files.
- [ ] Feature 20 adds funding to own recorder days, after which own runs can drop the "before funding" label.
- [ ] If the Milestone 1 gate shows the pessimistic queue far from reality on fills seen live later, consider a queue model that credits cancels ahead (a new spec).
- [ ] If throughput is still too slow for feature 15's day counts, consider a depth band filter in the hour stream (send the engine only levels near the top); it needs its own fidelity proof (identical fills against the full book).
- [ ] `/sync`: add `apps/lab/AGENTS.md` and the root context file pointer once the app exists; note in root `AGENTS.md` that no Agent Skill or MCP server exists for `tardis-dev` (searched 2026-10-08), so nothing offers one again.
