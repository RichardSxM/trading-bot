# 0014. Backtest hour level strategies on Kraken's 1 minute candle history, through the same engine and strategy code as the paper loop

**Date**: 2026-10-17
**Status**: Proposed

## Summary

The candle lab downloads Kraken Futures' free 1 minute BTC perp candles and hourly funding rates since the contract launched (March 2022) into a Parquet folder on the laptop. It then backtests hour level strategies over any date range. Each minute becomes four price points (open, the two extremes, close) fed to the NautilusTrader engine as quotes and trades, so the exact strategy classes the paper trader runs build their own hour bars and trade unchanged. Fees, a fixed slippage per fill, hourly funding, and forced liquidation are charged. A look ahead check and a candle versus order book parity check prove the results can be trusted. Results land in the same Postgres tables and reports as the scalping simulator, ready for the promotion gate (feature 15).

## Requirements

**User stories**:
- As the trader, I want to backtest an hour level strategy over years of real Kraken BTC perp history, with fees, funding, and slippage, so I only paper trade strategies whose edge survives costs.
- As the trader, I want the backtest to run the very strategy class the paper loop runs, so a backtest result says something about paper and live.
- As the trader, I want a strategy that peeks at the future to fail loudly, so a too good result is never a look ahead bug.
- As the trader, I want buy and hold and a simple MACD baseline scored the same way, so every candidate has a benchmark to beat.
- As the trader, I want to sweep a few parameter values in one command, so I can compare settings without editing files by hand.

**Acceptance criteria**:
- **AC-1** (candle fetch): `lab fetch-candles <from> <to>` takes UTC months (`YYYY-MM`, both inclusive). A month before `2022-03` or after the current UTC month is refused before any request (exit 64). For each month in order it pages the Kraken Futures charts endpoint for `trade` candles of `PF_XBTUSD` at resolution `1m`, from the month's start to the month's end or the fetch's start time, whichever is earlier, and writes `catalog-lab/candles/data/bar/PF_XBTUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL/<YYYY-MM>.parquet`. Each row is a Nautilus `Bar` with open, high, low, close as `Price` at the pinned instrument's price precision and volume as `Quantity` at its size precision, and `ts_event` = `ts_init` = the minute's close (its open + 60 s), sorted by `ts_init`, one row per minute Kraken returns. Rows that repeat across page edges are kept once when identical. A month whose end is before the fetch's start is written with `complete: true`. The running month gets `complete: false` and is fetched fresh on every run, and so is any month whose manifest says `complete: false`. A complete month is skipped (`lab_candles_month_skipped`) unless `--refetch` is given. `manifests/<YYYY-MM>.json` is written last, as the month's commit point. A month killed partway (no manifest) is fetched again from scratch. The pinned instrument has `price_increment` 1 (precision 0) and `size_increment` 0.0001. Gate G-1 records the price and volume decimals Kraken returns across the whole history. If any month is off the pinned tick or step, Milestone 2 does not start and the precision rule comes back to `/architect` (pin an instrument per period, or round and count). Until then the AC-3 refusal stands.
- **AC-2** (funding fetch): the same command fetches Kraken's historical funding rates for `PF_XBTUSD` (one public request, kept for the whole command) and writes each month's rows to `data/funding_rate_update/PF_XBTUSD.KRAKEN/<YYYY-MM>.parquet` as Nautilus `FundingRateUpdate` with `rate` = Kraken's relative funding rate (the same fractional per hour meaning as spec 0009's recorded rate, confirmed in the gate), `ts_event` = `ts_init` = the time the rate is charged, and `next_funding_ns` = that time + 1 h. Gate G-2 settles which period Kraken's timestamp names and when the rate is known. The row stamped at boundary B must be the rate charged at B, known at or before B: when Kraken stamps a rate with the start of the period it applies to, the row is stamped at that period's end. The file is written before the month's manifest.
- **AC-3** (row checks): before a month is written, every candle is checked: open, high, low, close all above zero and on the instrument's price tick, volume at or above zero and on its size step, `high` ≥ max(open, close), `low` ≤ min(open, close), the timestamp on a whole minute inside the month, and no two different candles for one minute. Every funding row is checked: timestamp on a whole hour, rate a finite decimal. Any failure stops the command with exit 1 and logs ERROR `lab_candles_bad_rows` listing every bad row of that month (minute, field, value). No file and no manifest is written for that month, and months already committed stay.
- **AC-4** (gaps): a run of five or more consecutive minutes with no candle is written as a row (`start_ns`, `end_ns`, `minutes`) in `gaps/<YYYY-MM>.parquet`. Missing minutes are never filled with made up candles. The manifest holds `month`, `bar_rows`, `funding_rows`, `first_ts_ns`, `last_ts_ns`, `missing_minutes`, `gap_count`, `missing_funding_hours` (whole hours inside the fetched span with no funding row), `complete`, and `fetched_at` (UTC ISO 8601).
- **AC-5** (network): requests are public GETs with no credentials, made with the Python standard library (`urllib.request`), a 30 s timeout, and a pause of `LAB_KRAKEN_PAUSE_MS` (default 250) between requests. A timeout, connection error, HTTP 429, or 5xx is retried up to 5 times, waiting 1, 2, 4, 8, then 16 s, and logs `lab_fetch_retry` each time. When retries run out, or on any other 4xx or a body that is not the expected JSON, the command logs ERROR `lab_fetch_failed` (URL without query, status, reason) and exits 1. Committed months stay and the failed month has no manifest. Before each month, the catalog cap check of spec 0008 AC-2 runs with the candles tree counted (expected month size 0.1 GB), and exits 3 when it would pass `LAB_CATALOG_CAP_GB`.
- **AC-6** (backtest input): `lab backtest <runfile> [--from D] [--to D] [--seed N] [--sweep-slippage] [--lookahead-check]` validates the run file (the model under *Run file*) and every override at startup. A bad file or value exits 64 with every problem listed, before anything is read or written. The range is UTC days, both inclusive. It then checks that every month the range touches has a manifest, that the range ends no later than the last fetched minute of the last month, that every hourly boundary strictly inside the range has a funding row with `ts_init` in `(boundary − 1 h, boundary]`, and that a funding row exists with `ts_init` in `(range start − 1 h, range start]` (it warms the strategy's market context through `warm_market_context` before the first bar, so the first entry is not refused as `funding_stale`). Otherwise it lists every missing month, the last fetched minute, or the first ten boundaries without a rate, and exits 2, writing no row, report, or bundle.
- **AC-7** (variants and grid): with no flag the run has one variant, `base`. `[strategy.grid]` maps strategy config fields (not also set in `[strategy.config]`, not run fields) to non empty lists. Their cross product, in sorted key order with each list in file order, gives variants `grid-1`, `grid-2`, … (at most 16, else exit 64), replacing `base`. `--sweep-slippage` adds `slippage=0` (0 bps) and `slippage=2x` (twice `slippage_bps`) next to `base`. It is refused with a grid (exit 64) and adds nothing when `slippage_bps` is 0. Each variant is its own `strategy_runs` row with its own `strategy_versions` row (the config hash includes the grid values), and `run_config` carries `batch_id`, `variant`, and `grid_point` (the grid values, or `{}`). The expansion is pure, in `trading_core/lab/variants.py` as a new `CandleVariantSpec` (name, `slippage_bps`, `grid_point`), since spec 0008's `VariantSpec` is built around latency and queue. Variants start in plan order and their bundles and reports are written in that order, as spec 0008 AC-6. The worker pool runs one variant per unit, at most `LAB_WORKERS` at a time.
- **AC-8** (engine): each variant runs in one fresh `BacktestEngine` over the whole range (from the first minute of `from` to the last minute of `to`), with no split at midnights or gaps. The venue is `KRAKEN` with `book_type=L1_MBP`, `oms_type=NETTING`, `account_type=MARGIN`, `MakerTakerFeeModel` on a copy of the pinned instrument with the run's `maker_fee` and `taker_fee`, no latency model, `queue_position=False`, `liquidity_consumption=False`, `trade_execution=False`, `bar_execution=False`, `use_reduce_only=True`, and the run's `starting_balance_usd` and `leverage`. `trader_id` is `BACKTESTER-LAB`.
- **AC-9** (candle stream): the stream turns each stored 1 minute candle (open time `m`) into four path points at `m + 1 ms` (open), `m + 20 s` (first extreme), `m + 40 s` (second extreme), `m + 59.999 s` (close). The extremes' order follows the strategy's net position as of the last processed path point when the minute is generated: long, low first; short, high first; flat, whichever extreme is nearer the open first (the high on a tie). A small `PositionSideFlag` actor sets that side from the engine's position events into a plain object the stream reads, and the stream builds one minute per chunk. Stated exception: in the minute an entry fills, the order was chosen while flat, so stop first is not guaranteed inside that one minute. Gate G-3 proves the flag lags by at most one path point. At each point it first emits a `TradeTick` at the point's `ts_init` and price, `NO_AGGRESSOR`, `trade_id` = `<ts_init>-<point index>`, with size = max(size step, volume ÷ 4 floored to the size step) at the first three points and max(size step, the remainder) at the close (so the four sizes may add up to more than the candle's volume, which only affects bar volume). Then it emits a `QuoteTick` at the same `ts_init` with bid = price × (1 − s) rounded down to the tick and ask = price × (1 + s) rounded up to the tick, where s = the variant's `slippage_bps` ÷ 10 000, and both sizes `QUOTE_SIZE` (a named constant, 1000 BTC at the size precision, far above any order `check_entry` can approve; the pinned instrument has no maximum quantity). The trade comes before the quote so that an L1 book update from the trade can never erase the slippage spread (gate G-3 asserts a market buy fills at the ask). A minute with no candle emits nothing. Funding rows are merged in by `ts_init` (before the trade at the same nanosecond). After the range's last minute, the stream emits one final quote repeating the last close at the last point + 2 ms (AC-14). The stream holds at most one month of candles in memory and is fed through `add_data_iterator`.
- **AC-10** (fills): for a strategy that decides on a bar close, a market buy fills at the ask of the path point current when the order reaches the venue (the minute's close point for an order sent on the close), and a sell at the bid. So every market fill pays `slippage_bps` against the candle price, plus the taker fee. A stop market order triggers on the first path point whose quote crosses its trigger, and fills at that quote. A limit order fills only when the opposite quote crosses its price (price trades through by the slippage, never a touch), and pays the maker fee. A position whose stop and target both lie inside one candle's range closes at the stop (the path order of AC-9, with its entry minute exception). Gate G-3 confirms in the engine that a triggered stop fills at the quote, not at its trigger price, and that a resting limit fills only on trade through. If the engine behaves otherwise, this AC comes back to `/architect` before Milestone 2.
- **AC-11** (same code): `lab backtest` runs the very `KfsStrategy` subclass the paper loop would run, built from the run file's `[strategy.config]` through the lab registry (spec 0008 AC-7 rules: the trader's own constructor arguments, a loaded and unpaused `PauseGate`, the real `EntryBlocks`, the `UnitRows` record callable, `check_entry` unchanged with the run's `RiskLimits`, the `LabRecorder` actor, no subclass or patch). Each candle strategy's bars come from its config field `bar_spec` (for example `1-HOUR-LAST`; only a time step of `MINUTE`, `HOUR`, or `DAY` with price type `LAST` is accepted, anything else exits 64), subscribed as `<instrument_id>-<bar_spec>-INTERNAL`, so the strategy aggregates bars from trade ticks the same way in the candle lab, in the order book replay, and live. A strategy with `use_market_context` receives the funding rows through its usual handler (mark, index, open interest, and liquidations are absent and read as missing). Registry entries carry a strategy kind. `buy_and_hold` and `macd_baseline` are `hour_level`, and `lab backtest` refuses (exit 64) a strategy whose kind is not `hour_level`.
- **AC-12** (buy and hold): `buy_and_hold` (pure rule in `trading_core/strategy/buy_and_hold.py`, node class `trading_core/node/buy_and_hold.py`) buys once at the first `bar_spec` bar close of the run, with `fixed_quantity` = `notional_usd` ÷ the last mid, floored to the size step, through `check_entry`, and holds with no stop until the range end close. Config: `bar_spec` (default `1-HOUR-LAST`), `notional_usd` (required), `max_entry_attempts` (default 24), `use_market_context` true. When `check_entry` refuses, it tries again at the next bar close and records each refusal as a decision, at most `max_entry_attempts` times. After that it logs `entry_abandoned` once and stays flat for the rest of the run.
- **AC-13** (MACD baseline): `macd_baseline` (pure rules and indicators in `trading_core/strategy/macd_baseline.py` and `trading_core/strategy/indicators.py`, node class `trading_core/node/macd_baseline.py`) runs on each `bar_spec` bar close while flat. It enters long when the MACD histogram (fast EMA − slow EMA, minus its signal EMA, all on closes) crosses from ≤ 0 to > 0 and the close is above the trend EMA, and short in the mirror case. Long exits at market when the histogram is below its previous value, short when it is above (the histogram weakens). Entries use `fixed_quantity` = `notional_usd` ÷ mid through `check_entry`, judged with a stop `stop_pct` percent from the mid. When the entry fills, the real protective stop market order is placed `stop_pct` percent from the fill price (so the stop checked and the stop placed can differ by the fill's slippage). Config: `bar_spec` (`1-HOUR-LAST`), `fast` 12, `slow` 26, `signal` 9, `trend_ema` 50, `stop_pct` `"3"`, `notional_usd` (required), `use_market_context` true. Each EMA uses `Decimal` with α = 2 ÷ (n + 1), seeded with the simple mean of its first n inputs. No decision is made until the histogram and trend EMA both have a value (warmup happens inside the range, 50 bars for the trend EMA with the defaults, and the report shows the first decision's time).

  Shared by both strategies: every `EntryRequest` field is sourced as `PlaceholderTimed` sources it (equity from the account balance plus `guard.unrealized`, `open_notional` from `guard.open_notional`, `instrument_maintenance_rate` from `guard.margin_maint`, `size_step` from the instrument, `paused` and `halted` from the `PauseGate`, `blocks` from `EntryBlocks`, `funding_rate` from `market_context()`, `funding_periods` = `funding_periods(now, None)` = 1). Both use the shared stale quote block (`stale_quote_s`, default 10), so no entry is made inside a candle gap. Both handle a position closed from any source (their own exit, the range end closer, or the liquidation guard): they cancel their resting stop and reset their entry state.
- **AC-14** (range end, liquidation, ruin): the range end closer (the spec 0008 AC-8 closer, with the reason passed in) fires at the last path point + 1 ms. It cancels the strategy's resting orders and closes any open position with a market order, after recording a `decisions` row with intent `exit` and reason `range_end`. The order fills against the final quote of AC-9 (the last close, at + 2 ms), so the close matches the buy and hold benchmark's last price. A `LiquidationGuard` actor watches every quote while a position is open. Its liquidation price comes from `trading_core.risk.liquidation.liquidation_price` with: side mapped to `recording.enums.OrderSide`; the position's quantity and average entry price; `equity` = the `figures.equity` of the approved entry decision's `check_entry` verdict, read from the strategy's recorded decision; other notional 0; `fee` = `taker_fee × quantity × entry price` in USD; and `rate` = the run's `maintenance_margin_rate` through `maintenance_rate` with the instrument's rate. Funding is not part of the engine's equity (the lab books it after the run), so it does not move the liquidation price. When a long's bid falls to or below it, or a short's ask rises to or above it, the guard records a decision with intent `exit` and reason `liquidated`, then closes the position at market on that quote through the strategy's own `order_factory` and `submit_order` (as `SegmentEndCloser` does). The report counts `liquidations`, and the strategy may enter again later (tested: a liquidation followed by a new entry). If account equity falls to zero or below at any point, the runner flattens, ends the variant early, and the report says `ruined: true` with the time. This is a result, not a failure.
- **AC-15** (funding): at every hourly boundary strictly inside the range, an open position pays `−signed_qty × mark_px × rate` through the existing `LabRecorder` samples and `trading_core.lab.funding.apply_funding`, with `mark_px` the mid of the last quote at or before the boundary and `rate` picked by `rate_at` from the candles tree's funding rows. Each payment is a `funding_payments` row (source `computed`) and counts in `positions.funding_total`, `run_results.funding_total`, and the net P&L, exactly as spec 0008 AC-9. `run_config.funding_source` is `"kraken_history"`.
- **AC-16** (results): before its first variant, `lab backtest` upserts the pinned instrument row and, per variant, the strategy version (kind `hour_level`). A completed variant calls `write_run_bundle` once as `lab_app` with mode `backtest`, env `demo`, status `completed`, its decisions, risk events, orders, order events, fills, positions, funding payments, and one `run_results` row (sample `full`, from `trading_core.lab.metrics.run_metrics`), with no `run_fill_stats`. `run_config` holds the spec 0008 fields that apply (`batch_id`, `variant`, `run_file`, `run_file_sha256`, `overrides`, `seed`, `instrument_id`, fees, `starting_balance_usd`, `leverage`, `risk_limits`, `nautilus_version`, `git_commit`) plus `source: "candles"`, `slippage_bps`, `grid_point`, `bar_type` (the stored bar type), `funding_source`, `candle_gaps` (each gap row overlapping the range), and `range` (`[start_ns, end_ns]`). `run_results.max_drawdown` keeps spec 0008's meaning (realized equity only) and is labelled "realized" in the report. The report JSON and Markdown add, from pure functions in `trading_core.lab.candle_metrics`: a marked to market equity curve (sampled at every hour close: realized equity plus the open position's P&L at that close's mid, plus funding booked so far) with its max drawdown and the longest drawdown duration (hours from a peak until equity is back above it, or until the range end); long and short split (closed trades and net P&L each); exposure time (share of the range with a position open); profit factor (gross profit ÷ gross loss, NULL when there is no loss); expectancy per closed trade (net P&L ÷ trade count); `liquidations`, `ruined`, and the first decision time; and a data quality block (span covered, missing minutes, gap rows, missing funding hours, from the manifests). These live in the report only, not in `run_results`. It writes `lab-reports/<batch_id>/<variant>.md`, `.json`, and `.bundle.json`, then `index.md` listing every variant side by side with net P&L, return after costs, realized and marked max drawdown, fees, funding, trade count, exposure time, liquidations, first decision time, and the buy and hold benchmark (`(last close − first close) ÷ first close` of the range's candles, before costs, shown for comparison only).
- **AC-17** (determinism): two runs with the same run file, overrides, data, and seed give identical report JSON (except `run_id`, `batch_id`, `started_at`, `ended_at`) and identical rows apart from ids and those timestamps, with `LAB_WORKERS` 1 or 2. This is the spec 0008 AC-12 rule applied to the candle path: `CandleUnitPlan` and `CandleUnitResult` (new, since spec 0008's `UnitPlan` and `UnitResult` are shaped around segments and hours) and the candle plan, bundle, and `run_config` builders in `apps/lab/src/lab/candle_backtest.py`, merging rows by (`ts`, sequence within the unit).
- **AC-18** (failure and interruption): a variant fails, is isolated, and is reported with the same rules as spec 0008 AC-13, through the candle path of AC-17 and the existing `UnitPool` (no database rows, `status: failed`, ERROR `lab_variant_failed`, the others still run, exit 1 if any failed). SIGINT or SIGTERM ends the command with exit 130 after writing `interrupted` reports and `index.md`.
- **AC-19** (look ahead): a strategy never receives a bar before the last path point of that bar's final minute has been fed. With `--lookahead-check`, the batch's first variant (`base` or `grid-1`; look ahead is a property of the code, not of a parameter value), after its full run, is run again three times on the same data cut at three times drawn from `random.Random(seed)` uniformly over the range's whole hours (excluding the first and last 10 %). Every decision of the full run with `decided_ns` strictly before a cut (`decided_ns < cut`), other than `range_end`, must equal one of the cut run (same intent, side, reason, `decided_ns`, risk verdict) and the cut run must have no extra one. A mismatch fails that variant (AC-18 rules), logs ERROR `lab_lookahead_mismatch` with the cut and the first differing decision, and the report shows it. A pytest test runs the same prefix check on every registered candle strategy over a seeded synthetic month of candles cut at five points. The check catches any peek at data the engine has not yet fed (through the stream, the strategy's buffers, or `SwingTracker`); it cannot catch a strategy that reads data from outside the engine, which the registry rule (strategies get data only through the engine) forbids.
- **AC-20** (swing helper): `trading_core/strategy/swings.py` provides pure `SwingTracker(left, right)` with `update(index, high, low) -> list[Swing]`. A swing high at bar `i` is a bar whose high is strictly above the highs of the `left` bars before it and the `right` bars after it (a swing low mirrors it with lows). It is returned only from the `update` call for bar `i + right`, as `Swing(kind, bar_index=i, price, confirmed_index=i + right)`, never earlier. Unit tests prove that no swing is ever returned before its confirming bar, and that feeding a prefix of the bars returns exactly the swings of the full feed whose `confirmed_index` lies inside the prefix.
- **AC-21** (parity): `lab candle-parity <runfile> [--from D] [--to D]` plans the `own` order book segments of the range (spec 0008 AC-4 planner). For each segment, it runs the strategy in memory twice over the same start and end: once through the spec 0008 order book unit (base variant, the run's fees, `queue_position` true, latency from the run file's optional `[parity]` table, `latency_ms` default 25) and once through the candle unit of this spec. `candle-parity` builds the order book side's unit plans from the own tree itself (it does not go through `lab replay`'s run file or its strategy kind rule, which stay unchanged). `[parity]` is allowed in a candle run file and read only by `candle-parity`. It matches `enter` decisions, approved or refused: same side and `decided_ns` within 60 s. It prints matches ÷ max(order book decisions, candle decisions) and every unmatched decision, writes nothing to Postgres, exits 1 under 0.95, and exits 2 when own days or candle months are missing or no decision is left to compare. The committed run file `apps/lab/runs/macd_parity.toml` runs `macd_baseline` with `bar_spec = "1-MINUTE-LAST"` (enough decisions over a few recorded days) and `use_market_context = false`, so both sides see the same inputs (the order book side would otherwise also get mark, index, open interest, and liquidations). Only `enter` decisions are compared. One missed cross can shift the following entries (a `position_open` refusal on one side only), which the 0.95 threshold has to absorb, and verify.md records the unmatched list.
- **AC-22** (guards): the lab still exits 78 with `TRADING_ENV=live` and on an image built without `GIT_COMMIT`. `fetch-candles` and `candle-parity` never connect to Postgres (run with `--no-deps`), and `backtest` connects only as `lab_app`, whose existing grants cover every table it writes (no migration). No Kraken key is mounted or read, and every Kraken request is an unauthenticated public GET to `LAB_KRAKEN_FUTURES_URL`.
- **AC-23** (speed and memory): one variant of `buy_and_hold` over the full history (2022-03-22 to the last complete month) finishes in under 30 minutes on the laptop, and its `ru_maxrss` stays under 1.5 GB inside the `lab` container's `mem_limit: 3g`. A full fetch of every month from 2022-03 finishes in under 30 minutes. A 16 point grid over the full history with `LAB_WORKERS=2`, and the same run with `--lookahead-check`, are timed too (measured only, no budget). All of these go into verify.md.

## Decision

**Chosen option**: Option 2: synthetic quote and trade path from 1 minute candles into the existing lab's `BacktestEngine`, with our own candle catalog fetched from Kraken's public history.

Store Kraken's 1 minute trade candles and hourly funding rates as monthly Parquet, turn each minute into four position aware price points with the slippage built into the quoted spread, and run the paper loop's strategy classes on them in a new `lab backtest` command that reuses the lab's pool, registry, recorder, funding, metrics, bundle, and reports.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `uv` (`terminalskills/skills`, `.claude/skills/uv/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

### Pipeline

```
Kraken charts API (1m trade candles) ─┐
Kraken historical funding API ────────┴─ lab fetch-candles ──► catalog-lab/candles/ (month files, gaps, manifests)
                                                                     ▼
run file (TOML) ──► lab backtest ──► range checks ──► variants (base | grid-n | slippage sweep)
                                                                     ▼
              worker pool (LAB_WORKERS, one spawned process per variant):
                 candle stream (4 path points per minute: trade, then quote with slippage spread,
                 funding rows) ──► BacktestEngine (L1, fees, same KfsStrategy + check_entry,
                 LabRecorder, PositionSideFlag, LiquidationGuard, range end closer) ──► CandleUnitResult
                                                                     ▼
              parent: funding, metrics, [look ahead cuts] ──► bundle ──► laptop Postgres (lab_app)
                                                                     └► lab-reports/<batch>/<variant>.md|.json|.bundle.json, index.md
own catalog + candles ──► lab candle-parity (in memory, order book unit vs candle unit)
```

### Data model sketch

**New Parquet tree `catalog-lab/candles/`** (laptop only, counted in `LAB_CATALOG_CAP_GB`):

| File | Rows | Fields | Rules |
|---|---|---|---|
| `data/bar/PF_XBTUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL/<YYYY-MM>.parquet` | Nautilus `Bar` | open, high, low, close (Price, required), volume (Quantity, required), ts_event = ts_init = minute close (ns, required) | sorted by ts_init; one row per minute; AC-3 checks; never filled in |
| `data/funding_rate_update/PF_XBTUSD.KRAKEN/<YYYY-MM>.parquet` | Nautilus `FundingRateUpdate` | rate (Decimal), next_funding_ns, ts_event = ts_init (ns) | one row per Kraken funding timestamp in the month; same table name as spec 0009 |
| `gaps/<YYYY-MM>.parquet` | gap rows | start_ns, end_ns (the missing span), minutes (int) | runs of 5+ missing minutes (AC-4) |
| `manifests/<YYYY-MM>.json` | one per month | AC-4 fields | written last; its presence commits the month |

Each month's manifest relates 1:N to its bar, funding, and gap rows. File writes go through `trading_core.catalog`'s `atomic_write`/`durable_replace`. The Parquet write and read helpers live in `trading_core/catalog/candles.py` (the only place besides `node/` allowed to touch Nautilus and pyarrow).

**Postgres: no migration.** Reused unchanged from spec 0002: `instruments`, `strategies` (kind `hour_level`), `strategy_versions` (one per variant config), `strategy_runs` (mode `backtest`, env `demo`, `trader_id` `BACKTESTER-LAB`), `run_results` (sample `full`), `decisions`, `orders`, `order_events`, `fills`, `positions`, `funding_payments` (source `computed`), `risk_events`. `run_config` contents are in AC-16. Code change: the `Store` protocol's `prepare` (and `PostgresStore.prepare` in `apps/lab/src/lab/replay.py`, plus the test fakes) gains a `kind` argument, taken from the registry entry instead of the hard coded `SCALPING`; `lab replay` passes its strategies' kind, which is still `scalping` for every strategy it runs today.

### Run file (TOML, `apps/lab/runs/*.toml`, model `CandleRunFile` in `apps/lab/src/lab/runfile.py`)

```toml
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2022-04-01                    # UTC day, inclusive
to = 2026-09-30                      # UTC day, inclusive
seed = 7
maker_fee = "0.0002"                 # strings, parsed as Decimal
taker_fee = "0.0005"
slippage_bps = "2"                   # per side, in [0, 100]
starting_balance_usd = "10000"
leverage = "2"

[risk]                               # RiskLimits, same RiskSection as spec 0008
max_order_notional = "20000"
max_leverage = "2"

[context]                            # optional, ContextSection defaults (max_age_funding_s 7200)

[strategy]
name = "macd_baseline"               # a registry key whose kind is hour_level
[strategy.config]
notional_usd = "10000"
[strategy.grid]                      # optional, at most 16 combinations
fast = [8, 12]
slow = [21, 26]

[parity]                             # optional, read only by lab candle-parity
latency_ms = 25
```

Extra keys are forbidden, `from` ≤ `to`, `from` ≥ 2022-03-22, fees in [0, 1). `[strategy.config]` and each grid point are validated by building the strategy's own config. `liquidations_available` is false. Committed run files: `buy_and_hold.toml`, `macd_baseline.toml`, `macd_parity.toml`.

### API surface (CLI, `docker compose run --rm lab <command>`)

| Command | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|
| `lab fetch-candles <from> <to> [--refetch]` | months `YYYY-MM` (req) | `catalog-lab/candles/` months; `lab_candles_month_done` per month | none (public Kraken GETs) | 64 bad month; 1 bad rows or fetch failed; 3 catalog cap |
| `lab backtest <runfile> [--from D] [--to D] [--seed N] [--sweep-slippage] [--lookahead-check]` | run file (req), overrides (opt) | rows via `write_run_bundle`; `lab-reports/<batch_id>/` | `lab_app` Postgres role | 64 bad input; 2 missing months, data, or funding; 1 a variant failed; 130 interrupted; 78 live or unknown commit |
| `lab candle-parity <runfile> [--from D] [--to D]` | run file (req) | ratio, unmatched decisions | none (no DB) | 1 under 0.95; 2 missing data or nothing to compare |

`fetch-candles` and `candle-parity` run with `--no-deps`.

### Value sourcing

| Action | Value | Source |
|---|---|---|
| fetch-candles | candle OHLCV | Kraken charts endpoint, `trade` tick type, `1m` |
| fetch-candles | price precision, tick, size step | pinned instrument `apps/lab/src/lab/instruments/PF_XBTUSD.KRAKEN.json` (history fit checked in gate G-1) |
| fetch-candles | funding rate per hour, and its stamp | Kraken historical funding rates endpoint, relative rate field; meaning and the period its timestamp names confirmed in gate G-2 |
| backtest | quote sizes | `QUOTE_SIZE` constant, 1000 BTC (the pin has no maximum quantity) |
| backtest | synthetic trade sizes and ids | max(size step, volume ÷ 4 floored); `trade_id` = `<ts_init>-<point index>` |
| backtest | `check_entry` inputs | as `PlaceholderTimed`: account balance plus `guard.unrealized`, `guard.open_notional`, `guard.margin_maint`, `PauseGate`, `EntryBlocks`, `market_context()`, `funding_periods(now, None)` |
| fetch-candles | `complete` | month end < fetch start (wall clock UTC) |
| backtest | fees | run file `maker_fee`, `taker_fee` (defaults in committed files: 0.0002, 0.0005, Kraken base tier as in spec 0008) |
| backtest | slippage per side | run file `slippage_bps` (committed default 2); variants 0 and 2× |
| backtest | path order per minute | `PositionSideFlag`: the net position as of the last processed path point when the minute is generated (AC-9) |
| backtest | fill price | the synthetic quote at the triggering path point (AC-10) |
| backtest | funding mark price | mid of the last quote at or before the boundary (= the previous path point's candle price) |
| backtest | liquidation price | `risk.liquidation.liquidation_price` with `figures.equity` of the approved entry verdict, fee = `taker_fee × qty × entry_px` (USD), `RiskLimits.maintenance_margin_rate` via `maintenance_rate` |
| backtest | marked equity, drawdown duration, exposure, long and short split, profit factor, expectancy | `trading_core.lab.candle_metrics` from fills, positions, funding, and hour close mids |
| backtest | data quality block | the range's month manifests and gap files |
| backtest | strategy funding input | `funding_rate_update` rows fed into the engine, read through `market_context()`, stale after `[context].max_age_funding_s` (7200) |
| backtest | buy and hold benchmark | first and last candle close inside the range |
| backtest | metrics | `trading_core.lab.metrics.run_metrics` |
| backtest | look ahead cut times | `random.Random(seed)` over the range's whole hours |
| candle-parity | segments | spec 0008 `plan_segments` on the own tree |
| all | Kraken base URL | `LAB_KRAKEN_FUTURES_URL` |

### Key invariants

- No money value is a float: candle prices, rates, slippage, and P&L are `Decimal`, `Price`, `Quantity`, or `Money` from the API string to the report.
- No candle is ever invented. A missing minute emits no tick.
- A bar reaches a strategy only after its last minute's close point (AC-19).
- Every market fill is at least `slippage_bps` worse than the candle price. A stop never fills better than its triggering quote.
- A complete month on disk is never changed without `--refetch`. A month without a manifest is never read.
- Strategy classes are never subclassed or patched for the lab.

### Security model

Laptop only, demo only. No secret beyond the existing `postgres_lab_password`. Kraken requests are public, unauthenticated GETs, and no key exists on the laptop (spec 0003). `lab_app` keeps SELECT and INSERT on the bundle tables only. No regulated data.

### Configuration required

- `LAB_KRAKEN_FUTURES_URL`: base URL of the public Kraken Futures API (default `https://futures.kraken.com`), validated as an https URL in `LabSettings`.
- `LAB_KRAKEN_PAUSE_MS`: pause between requests (default 250, 0 to 5 000).
- The candles tree lives under the existing `LAB_CATALOG_DIR` mount (`/catalog-lab/candles`), so the Compose service needs no new volume.

### Critical test scenarios

- Happy path: fetch two months, backtest `buy_and_hold` and `macd_baseline` over them, rows and reports written with fees, slippage, and funding, verifies **AC-1**, **AC-2**, **AC-12**, **AC-13**, **AC-15**, **AC-16**
- Fill rules on a hand built candle fixture: market at close ± slippage, stop first when both inside one candle, limit only on trade through, verifies **AC-9**, **AC-10**
- Look ahead: a test strategy that reads the next bar fails both the pytest prefix check and `--lookahead-check`, verifies **AC-19**
- Failure: a bad candle row stops the month with every bad row listed, a 429 storm retries then exits 1 keeping earlier months, verifies **AC-3**, **AC-5**
- Liquidation: a 2x long through a fixture crash closes at the guard with reason `liquidated`, verifies **AC-14**
- Permission: `TRADING_ENV=live` exits 78; `lab_app` cannot write outside the bundle tables, verifies **AC-22**

## Build plan

Tracer Bullet: a gate first (the engine behaviors this design leans on), then one real month end to end through every layer, then thicken.

**Milestone 1: Gate** (`scripts/spikes/0014_gate.py`, results into rationale.md)
1. G-1: page the charts endpoint for one month: page size, the paging field, earliest available minute, what a no trade minute returns. Sample one month per year from 2022 and record the price and volume decimals against the pinned tick and step (an off tick result stops here and goes back to `/architect`, AC-1). Satisfies **AC-1**, **AC-3**, **AC-4**
2. G-2: fetch funding history; compare the relative rate with spec 0009's recorded rate and Tardis `derivative_ticker` on overlapping hours, and settle which period a timestamp names and when the rate is known (the stamp rule of AC-2). Satisfies **AC-2**, **AC-15**
3. G-3: in a `BacktestEngine` with the AC-8 venue: a timer at T runs before data at T + 1 ms; with the trade before the quote at each point, a market buy on a bar close fills at the close point's ask; a triggered stop fills at the quote, not its trigger; a limit fills only on trade through; a zero size `TradeTick` is refused; `1-HOUR-LAST-INTERNAL` bars build from iterator fed trades, including the first partial hour and an hour with no candles; `PositionSideFlag` lags by at most one path point with one minute chunks (throughput measured). Any result against AC-9 or AC-10 goes back to `/architect`. Satisfies **AC-8**, **AC-9**, **AC-10**, **AC-11**

**Milestone 2: Thin thread** (one month, `buy_and_hold`)
4. `trading_core/catalog/candles.py` (write, read, manifest) and pure candle rules in `trading_core/lab/candles.py` (month list, completeness). Satisfies **AC-1**, **AC-2**
5. `lab fetch-candles` happy path with the standard library client and the new settings. Satisfies **AC-1**, **AC-2**, **AC-22**
6. Candle stream `apps/lab/src/lab/candle_stream.py` with the pure path rule in `trading_core/lab/candle_path.py`, `QUOTE_SIZE`, trade sizes and ids, the final close quote, and the `PositionSideFlag` actor. Satisfies **AC-9**, **AC-10**
7. Candle unit and venue (`apps/lab/src/lab/candle_units.py`, `CandleUnitPlan`, `CandleUnitResult`), registry kind field, `Store.prepare` kind, range end closer reason and timing, `bar_spec` validation. Satisfies **AC-8**, **AC-11**, **AC-14**
8. `buy_and_hold` pure rule and node class (shared `check_entry` inputs, attempt cap, outside close handling), registered. Satisfies **AC-12**, **AC-13**
9. `lab backtest` run file model, range checks (including the warmup funding row), funding, the candle plan and bundle path in `apps/lab/src/lab/candle_backtest.py`, reports with the benchmark and `trading_core/lab/candle_metrics.py`, `buy_and_hold.toml`. Satisfies **AC-6**, **AC-15**, **AC-16**, **AC-17**

**Milestone 3: Data you can trust**
10. Row checks, gap rows, manifest fields. Satisfies **AC-3**, **AC-4**
11. Retries, backoff, pause, failure exits, catalog cap, `--refetch`, the running month. Satisfies **AC-1**, **AC-5**
12. Fetch every month since 2022-03 and time it. Satisfies **AC-23**

**Milestone 4: A real strategy**
13. `trading_core/strategy/indicators.py` (Decimal EMA, MACD) and `swings.py` with their unit tests. Satisfies **AC-13**, **AC-20**
14. `macd_baseline` pure rules, node class, run file. Satisfies **AC-13**
15. `LiquidationGuard` actor, the ruin stop, and the liquidation then re-entry test. Satisfies **AC-14**
16. Grid and `--sweep-slippage` variants (`CandleVariantSpec`, pure expansion in `trading_core/lab/variants.py`). Satisfies **AC-7**

**Milestone 5: Proof**
17. Pytest prefix check over every candle strategy and `--lookahead-check`. Satisfies **AC-19**
18. `lab candle-parity` and `macd_parity.toml`, run on the own days. Satisfies **AC-21**
19. Determinism test, failure isolation and interrupt tests. Satisfies **AC-17**, **AC-18**
20. Full history timing and memory into verify.md; boundary test still passes (`test_core_boundary.py` covers the new modules). Satisfies **AC-22**, **AC-23**

## Consequences

**Positive**:
- Four and a half years of the real contract, with funding, for free, in under 1 GB.
- The paper loop's strategy classes run unchanged, and parity against the order book replay proves it numerically.
- Slippage, stop first ordering, and forced liquidation lean pessimistic, so a passing strategy is unlikely to be a flattering artifact.
- No migration: feature 15 reads candle results from the same tables as replay results.

**Negative / tradeoffs**:
- Four points per minute hide the true path. A stop fills at the minute's extreme, not at its trigger, which overstates stop losses in fast minutes.
- No order book means no queue or partial fills. Maker limit fills are approximate (trade through only), so maker heavy hour strategies should be checked in the replay lab too.
- Mark, index, open interest, and liquidations have no history before our recording, so strategies that need them cannot be backtested on years of data.
- Warmup inside the range spends the first bars of every range (50 bars, the trend EMA, for the MACD baseline: about two days on 1h bars).
- In the minute an entry fills, the path order was chosen while flat, so stop first is not guaranteed inside that single minute.
- The marked drawdown, exposure, and the other report only figures are not in `run_results`, so feature 15 reads them from the report or the bundle until it decides otherwise.
- BTC only. ETH needs its own pinned instrument and a later fetch.
- Depends on a Kraken endpoint that may change shape or limits. The gate and the row checks catch it, but a change means a code fix.

**Neutral**:
- `lab replay`'s strategy kind stops being hard coded (a small change to spec 0008 code).
- New files in core: `catalog/candles.py`, `lab/candles.py`, `lab/candle_path.py`, `lab/candle_metrics.py`, `strategy/indicators.py`, `strategy/swings.py`, `strategy/buy_and_hold.py`, `strategy/macd_baseline.py`, `node/buy_and_hold.py`, `node/macd_baseline.py`.
- `apps/lab/AGENTS.md` needs the new commands and the candles tree (for `/sync`).

## Follow-up

- [ ] Feature 15: decide how walk forward windows call `lab backtest` (in sample and out of sample ranges as separate runs), and whether the report's buy and hold benchmark should become a `run_results` row.
- [ ] Feature 17: build the hour candidates on `SwingTracker` and the indicators module; each must pass `--lookahead-check`.
- [ ] Feature 16: the 1 minute candle ideas (VWAP bounce, EMA pullback) can run here first, but their fills need the replay lab before promotion.
- [ ] ETH (`PF_ETHUSD`): a pinned instrument and a fetch, if feature 17 wants a second market.
- [ ] A volume aware strategy (above average breakout volume) relies on candle volume split over four trades; check it is enough when feature 17 needs it.
- [ ] The paper trader runs one hard wired strategy; running `macd_baseline` in paper needs multi strategy support (feature 19 or its own feature).
