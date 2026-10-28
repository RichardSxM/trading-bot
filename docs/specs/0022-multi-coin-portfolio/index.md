# 0022. Multi coin portfolio: `donchian_4h` on BTC and ETH in one shared account, through a fresh study and the gate

**Date**: 2026-10-28
**Status**: In Progress

## Summary

Every single BTC rule so far has failed the gate. The best one, `donchian_4h`, covered its costs but failed on luck and concentration checks: too few trades, and too much profit from a few of them. This spec runs the same rule on BTC and ETH at once, inside one backtest with one shared margin account, so the gate judges one combined equity curve with about twice the trades. The lab learns to run more than one coin, ETH gets its own candles and funding, the gate gains one check that stops a "portfolio" passing on one coin's luck, and everything else (thresholds, risk rules, the trader) stays as it is. The BTC half of the holdout was already seen by spec 0020, so the result is labelled that way.

## Requirements

**User stories**:
- As the trader, I want my best candle rule tested on BTC and ETH together, in one account the way Kraken would hold it, so I learn whether more trades and a second market turn a near miss into a real edge.
- As the trader, I want the gate to refuse a portfolio whose profit comes almost all from one coin, so diversification is real and not a label.
- As the trader, I want every existing single BTC study, run file, and verdict to stay exactly as it is, so past results stay valid.

**Acceptance criteria**:
- **AC-1** (hash baseline): before any code, `verify.md` records the sorted module hash list sha256 of every `trading_core.node` module at the commit this feature's branch starts from, from `scripts/spikes/hash_check.py` (spec 0020 AC-1 method). At close out (AC-18) the list on the branch matches it line for line, with no module added or edited: the multi coin wiring lives in `apps/lab`, never in a node.
- **AC-2** (ETH instrument and data): `apps/lab/src/lab/instruments/PF_ETHUSD.KRAKEN.json` is copied from the VPS catalog's newest `PF_ETHUSD` crypto_perpetual row, with `maker_fee` `0.0002` and `taker_fee` `0.0005` written in by hand and a `_note` saying so (the BTC file's method). `lab fetch-candles` and `lab import-funding` gain `--instrument <id>` (default `PF_XBTUSD.KRAKEN`, so every existing command and script runs unchanged). The fetch builds the Kraken symbol from the id's symbol part, and the import reads member `exports/<symbol>.csv` and accepts only that tradeable. ETH candles and funding pass the same checks as BTC's (spec 0014 AC-1 to AC-3: rounding to the pinned tick and size step, quiet minutes, gap rows, manifests). An id with no pinned file exits 64. `verify.md` records ETH's first candle minute, its first whole month, per year its missing minutes, gap rows, and missing funding charges, and the pin's `margin_maint` (the liquidation input `guard` reads) next to Kraken's published value.
- **AC-2a** (off tick open and close, amends spec 0014 AC-1 and AC-3 for every coin): Kraken's ETH candles hold two off tick closes, each repeated as the next minute's open (2023-08-17 22:55 and 2023-10-16 13:30 UTC; rationale.md, *Off tick ETH closes*). An open or close more than `1e-8` off the tick is no longer a bad row on its own. It is rounded to the nearest tick (`ROUND_HALF_EVEN`), and then, if that lies outside the candle's stored low to high (the highs and lows already rounded by spec 0014 AC-1), it is set to the nearer of the two, so no stored price lies beyond the range Kraken reported. `round_price` gives the nearest tick for an off tick open or close (no longer `None`), and `_store` clamps it once the high and low are stored; a field that is already a bad row skips the clamp. A repeated flat print: when a candle's four raw prices are equal and off the tick, its volume rounds to 0, and that raw price equals the previous candle's raw close, all four are stored as the previous candle's stored close (so it stays flat at the previous close for the quiet check). Any other candle whose stored low is then above its stored high (the whole candle inside one tick) is a bad row. After a clamp, a close and the next minute's open can differ by one tick (22:55 close 1660.6, 22:56 open 1660.7); no continuity check is added. The order stays spec 0014's: round all four prices first (now open and close too), then every other AC-3 check, the quiet decision, and the duplicate rule, all on the stored values (identical raw page edge copies stay identical). Such a candle counts once in `price_rounded_rows` and its open or close appears in `lab_candles_price_rounded` like a high or low; the manifest gains no field. A guard against a real tick change: a pre pass counts, per coin and per month, the candles whose raw open or close is more than `1e-8` off the tick (noise snaps excluded, candles bad for other reasons included). When that count is above `OFF_TICK_BODY_MAX` (10, a named constant in `trading_core/lab/candles.py`; a rule of thumb, since ETH shows at most 2 a month and a real tick change would put most candles off), each such open or close is one bad row entry (minute, field, raw value), listed with any other bad rows, and the month exits 1 as before. A `--refetch` does not recheck a month stored under the old rule (no ETH month was). No stored BTC candle has an off tick open or close (spec 0014 rationale), so no BTC month changes and none is fetched again. `trading_core/lab/candles.py` is outside every `trading_core.node` closure, so AC-1 holds.
- **AC-3** (run file): a candle run file holds exactly one of `instrument_id` (a string, as today) or `instrument_ids` (a list of at least 2 distinct pinned ids, sorted on load); both, neither, a duplicate, a single item list, or an unpinned id exits 64. A single coin file loads, plans, and hashes exactly as before: for every committed study file, `LoadedStudy.plan_hash` and each grid point's strategy version config are byte identical to the base commit's (a test pins the `donchian_4h.toml` and `macd_baseline.toml` values). A multi coin run's `run_config` carries `instrument_ids` (sorted) and no `instrument_id`. The lab adds the key `instrument_ids` to the strategy version config (`{**[strategy.config], **grid_point}` today, which holds no instrument) only for a multi coin run, so `entry.frozen` hashes it and the frozen hash differs from any single coin version of the same rule. The run file model dumps `instrument_ids` only when it is set (excluded when `None`), which keeps every single coin dump, and so `plan_hash`, unchanged. Every command that takes a candle run file states whether it allows `instrument_ids`; the loader refuses it where not allowed (AC-10). The first whole month of the shared range is never before 2022-04-01 (BTC's floor).
- **AC-4** (one engine, one account): a multi coin candle unit builds one `BacktestEngine` with one `KRAKEN` venue, one margin account at `starting_balance_usd`, and `leverage`, and adds each coin with the run's fees. `candle_tree(catalog, instrument_id)` takes the coin (the BTC only constant goes), and `CandleBatch` keeps one tree per coin for gaps, months, and stored funding. For each coin, in sorted id order, the unit builds through `RunContext` (whose `instrument_id` is per coin) one strategy instance from the same `[strategy.config]` and grid point, with `order_id_tag` `C1`, `C2`, … (single coin runs keep `C1`, and the tags keep strategy ids unique). Each coin also gets its own `PauseGate`, `EntryBlocks`, `bar_type`, `CandleStream`, `SegmentEndCloser` (at its own last path point plus 1 ms), and `LiquidationGuard`. Shared across coins: one `UnitRows`; one `LabRecorder` and one `PositionSideFlag`, each holding a per coin map (bar type, side flag, zero volume count) and routing every order and position event by its `instrument_id`, so no row is written twice and each coin's slippage side follows only its own position; and one ruin handler (below). The coins' streams are merged into one data iterator, `candles` (AC-7). The engine runs to the latest final quote time over the coins. A coin with no candle in the range fails the variant. A position still open after the range end close fails the variant, as today.
- **AC-4b** (shared ruin): when any `LiquidationGuard` finds account equity at or below 0, the shared ruin handler closes every open position on every coin at its last quote (taker, recorded with reason `ruin`), sets `ruined_ns`, and only then stops the engine, so the variant reports ruin instead of failing on an open position. A guard's equity reading needs a quote for every coin that has an open position; a coin with a position and no quote raises, as `guard.unrealized` does today.
- **AC-5** (sizing): every entry is sized by the existing `check_entry` from the shared account's equity, with `open_notional` summed over every open position on every coin (the existing `guard.open_notional`). `RiskLimits` gains no field. The second coin's entry is shrunk by `exposure_cap` (or refused by `zero_quantity`) exactly as a single coin's would be when the first coin's position uses the cap.
- **AC-6** (per coin data gaps): a stale quote or candle gap on one coin blocks only that coin's entries (its own `stale_quote_s` check), and the other coin keeps trading. Gaps, quiet minutes, zero volume bars, and missing funding are counted and reported per coin.
- **AC-7** (determinism): the coins' `CandleStream`s are merged into one `chunks()` iterator that, inside each chunk, orders points by timestamp and then by sorted instrument id, so equal timestamp events are processed in sorted id order by construction (no reliance on how Nautilus breaks ties between iterators). A single coin run's stream is unchanged. The same multi coin run twice gives byte identical rows, report JSON, and bundle (wall times and RSS left out, as spec 0014 AC-17). Tests prove the order and the byte identity on fixture candles for two coins.
- **AC-8** (funding): `FundingSample` stays unchanged, because `trading_core.lab.funding` is in the code closure of `trading_core.node.recording` and every node module that imports it (AC-1). The lab keeps samples per coin instead: `UnitRows.funding_samples` and `CandleUnitResult.funding_samples` become a map from instrument id to that coin's samples, with keys in sorted id order (a single coin run has one key; AC-7 needs the order). The `LabRecorder`'s single `_lab_instrument_id` becomes its per coin map (AC-4), and each coin's boundary sample is appended under that coin. The Tardis replay's `UnitResult` (`units.py`, `replay.py`) keeps its flat tuple, taken from the one map entry. `funding_pass` and `with_funding` take the coin's id and see only that coin's positions. `funding_pass`, `with_funding`, `expected_charge_times`, `carried_funding_charges`, and `stored_funding` run once per coin on that coin's samples (its map entry), positions, and funding tree, and the results are joined in sorted id order. The coin of a sample is its map key, so a position never takes another coin's rate.
- **AC-9** (report): `CandleUnitResult` gains `per_instrument`, a map from coin to its zero volume bars, first trade, last point, and events (the scalar fields keep their single coin meaning and hold the combined value for a pair: sum of events and zero volume bars, earliest first trade, latest last point). The run report JSON and Markdown keep every combined figure with its current meaning and add a `per_instrument` block. For each coin it shows closed trades, net P&L, fees, funding, long and short split, exposure time, zero volume bars, first and last candle, and its data quality block. Combined figures over the shared account: marked equity is `marked_equity` run per coin (its own fills, funding, and hour marks), combined as `starting balance + Σ (coin equity − starting balance)` on the union of hour closes, where a coin with no hour close yet adds 0; its max drawdown and longest drawdown come from that combined curve. Combined exposure is the share of the range with any position open (the union). The buy and hold figure for a multi coin range is the plain mean of the coins' `(last close − first close) ÷ first close`. `index.md` adds one column, `coins`.
- **AC-10** (refusals): the run file loader takes `allow_multi`; only `lab backtest`, `lab study`, and `lab holdout` pass `True`. `lab replay`, `lab screen`, the candle parity check, and a look ahead cut run refuse a run file with `instrument_ids`, exiting 64 with a message naming the key.
- **AC-11** (benchmarks): `Market` carries the study's `instrument_ids`, and `_benchmark_run` injects them, so `lab holdout` runs the committed `buy_and_hold` and `macd_baseline` benchmark files on every coin, in one shared account each, with no edit to the benchmark files. For `buy_and_hold`, the lab sets each coin's strategy config `notional_usd` after load to the file's value divided by the coin count (a 50/50 split at entry, held to the end, never rebalanced). `macd_baseline` sizes per coin from the shared equity, as the candidate does. `last_complete_day` takes the minimum over the coins' trees.
- **AC-12** (bundles carry instruments): `RunBundle` keeps `instrument` (the first coin in sorted order, so every reader that needs one still works) and gains `instruments`, the `InstrumentRow` of every coin in the run (for a single coin run, that one coin), written by `write_run_bundle` and the bundle codec. The lab's `store.prepare` upserts every coin. `gate import` upserts the union of `instrument` and `instruments` before the run's rows: a row with the same content is a no op, and different content refuses that file (`RecordConflictError`). A bundle written before this spec (no `instruments`) loads as it does today.
- **AC-13** (gate check): `TradeReturn` gains `instrument_id`; `holdout_trades` selects `Position.instrument_id` and orders by `closed_at`, `opened_ns`, then `instrument_id`, so ties are deterministic. Each trade return stays over the combined equity before it. A new check, `concentration_instrument`, takes the largest coin's share of holdout net profit (per coin sum of trade `net_pnl`). It fails when that share is above `instrument_max_share`, reason metric `instrument_share`. It is skipped when the holdout has trades on fewer than 2 coins, or when total net profit is at most 0 (another check already fails that, as the other concentration checks do). `thresholds.toml` becomes `version = 2`, adding `instrument_max_share = 0.60` with every other value unchanged. The loader accepts versions 1 and 2: in version 1, `instrument_max_share` must be absent and loads as `None` (the check is then skipped); in version 2 it is required. So every stored v1 thresholds JSON (`Thresholds.from_json` in `gate_queries` and `lab study`/`lab holdout`) still loads. Tests: a stored v1 thresholds JSON loads under the new code; an existing single coin fixture study evaluates under v2 to the same verdict and reasons as under v1.
- **AC-14** (study detail): the dashboard's `/strategies/{study_id}` reads `promotions.metrics["instrument_share"]` and shows it as "Coin share" beside the other concentration shares when present, and shows nothing new otherwise.
- **AC-15** (study file): `apps/lab/studies/donchian_4h_pair.toml` is `donchian_4h.toml` with `instrument_ids = ["PF_ETHUSD.KRAKEN", "PF_XBTUSD.KRAKEN"]` in place of `instrument_id`, `from` set to the first whole month both coins have candles (2022-04-01 unless AC-2 finds ETH starts later), the same `to`, fees, risk, fixed settings, and the 2 × 2 grid (`entry_bars` 20 and 55, `stop_atr` 2 and 3), and `[study] name = "donchian_4h_pair"`, `lab = "candle"`, `seed = 42`. Strategy name stays `donchian_4h`. It is committed, and the image rebuilt with `GIT_COMMIT`, before its first run. The development and holdout end days are the earliest last complete day over the coins.
- **AC-16** (run and verdict): the study runs through `lab study studies/donchian_4h_pair.toml` and `lab holdout donchian_4h_pair` on the laptop, never `lab backtest` on real candles. Then, on the VPS with `--no-deps`, each step confirmed with the trader first: deploy the gate and dashboard at thresholds v2, `scripts/push-bundles.sh`, and `gate evaluate donchian_4h_pair` exactly once. The family N (kind `hour_level`, lab `candle`) counts all 4 grid points.
- **AC-17** (results record): a Results section below gets, in its own commit `docs(specs): donchian_4h_pair verdict (0022)`, the fields of spec 0020 AC-10 (study, promotion id, evaluation date, verdict, every failed reason with value and threshold, walk forward Sharpe, holdout Sharpe against buy and hold, holdout return after costs, marked max drawdown, deflated Sharpe probability, PBO, holdout trades, N, gross and cost per trade). It also records per coin holdout trades and net P&L and the coin share, and carries the label "BTC holdout seen (spec 0020)". A fail is recorded the same way and never removed.
- **AC-18** (close out): after the verdict, the AC-1 hash comparison runs again, the scope feature's Build it box is ticked, and on a `pass` the paper follow up below is enrolled. No revision of `donchian_4h_pair` runs in this spec.

## Decision

**Chosen option**: Option 1: one rule on BTC and ETH in one shared account backtest.

Teach the candle lab to run one frozen rule on several coins inside one Nautilus engine and one margin account, study `donchian_4h` on BTC plus ETH with a fresh grid, and add one tightening gate check (`concentration_instrument`) so the combined curve is judged like any other run.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no migration.
| Store | Entity | Change |
|---|---|---|
| Postgres | `instruments` | new row `PF_ETHUSD.KRAKEN` (laptop through the lab's upsert, VPS through the bundle, AC-12) |
| Postgres | `strategy_runs.run_config` (JSON) | multi coin runs carry `instrument_ids` (sorted list) instead of `instrument_id` |
| Postgres | `strategy_versions` config | carries `instrument_ids` for a multi coin run, so its frozen hash is new |
| Postgres | `positions`, `fills`, `orders`, `order_events`, `decisions`, `funding_payments`, `risk_events` | reused; each row already names its coin (`instrument_id`, FK to `instruments`); one `strategy_run` holds every coin's rows |
| Postgres | `run_results`, `run_measures`, `study_runs`, daily equity | reused; one combined shared account curve per run |
| Postgres | `promotions.metrics` (JSON) | new metric `instrument_share` (AC-13) |
| Parquet | `data/bar/PF_ETHUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL/<YYYY-MM>.parquet` | new tree, BTC's shape and checks |
| Parquet | `data/funding_rate_update/PF_ETHUSD.KRAKEN/<YYYY-MM>.parquet` | new tree, from the export member `exports/PF_ETHUSD.csv` plus the API |
| Package file | `apps/lab/src/lab/instruments/PF_ETHUSD.KRAKEN.json` | new pinned instrument |
| Package file | `trading_core/gate/thresholds.toml` | `version = 2`, adds `instrument_max_share = 0.60` |
| Bundle file | `RunBundle.instruments` | new optional field (AC-12) |

**State transitions**: the `BarRuleStrategy` position cycle, unchanged and per coin: flat → entry (taker on the bar close) → open with stop → closed by stop, channel exit, time stop, or range end. The coins' cycles are independent except through the shared equity and exposure cap (AC-5) and the shared ruin stop (AC-4).

**Interface surface** (CLI):
| Command | Where | Key inputs | Key outputs | Key errors |
|---|---|---|---|---|
| `lab fetch-candles --instrument PF_ETHUSD.KRAKEN` | laptop | instrument id (opt, default BTC) | month Parquet files and manifests | 64 unpinned id; 1 bad rows |
| `lab import-funding <zip> --instrument PF_ETHUSD.KRAKEN` | laptop | Kraken funding export, instrument id (opt) | merged funding months | 64 missing member, wrong header or tradeable |
| `lab study studies/donchian_4h_pair.toml` | laptop | the AC-15 file | study batch, 4 grid runs over folds | 64 bad `instrument_ids`, plan changed under the same name |
| `lab holdout donchian_4h_pair` | laptop | study name | candidate, `buy_and_hold`, `macd_baseline` runs on both coins | 64 grid incomplete |
| `lab replay`, `lab screen`, parity, `--cut` | laptop | a multi coin run file | none | 64 `instrument_ids` not supported (AC-10) |
| `scripts/push-bundles.sh <dirs>` | laptop to VPS | batch folders | loaded, skipped, refused counts | refused > 0 (an instrument conflict, AC-12) |
| `gate evaluate donchian_4h_pair` | VPS, `--no-deps` | study name | verdict, reasons, promotion id | run once only |

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| ETH pin | tick, size step, margins, multiplier | VPS catalog's newest `PF_ETHUSD` crypto_perpetual row (the recorder records ETH) |
| ETH pin | maker and taker fee | written by hand, `0.0002` and `0.0005`, Kraken base tier, same as BTC (spec 0008 AC-11) |
| ETH fetch | Kraken symbol | the id's symbol part (`PF_ETHUSD`) |
| candle row | stored off tick open or close | nearest tick, kept inside the stored low to high; a bad row past `OFF_TICK_BODY_MAX` (10) per month (AC-2a) |
| funding pass | a sample's coin | the key of the per coin samples map the `LabRecorder` filled (AC-8) |
| ETH funding | rate and charge time | export member `exports/PF_ETHUSD.csv` (`relative_rate`, `timestamp`) plus the funding API, merged as for BTC |
| study | `from` | first whole month both coins have candles, from AC-2's `verify.md` record |
| study and holdout | last day | earliest last complete day over the coins (`last_complete_day` per coin, then the minimum) |
| unit | instance order and `order_id_tag` | sorted `instrument_ids`, `C1` upward (AC-4) |
| unit | equal timestamp order | the merged `chunks()` iterator: timestamp, then sorted id (AC-7) |
| unit | engine end | latest final quote time over the coins (AC-4) |
| version config | `instrument_ids` | added by the lab for multi coin runs only (AC-3) |
| report | combined marked equity, drawdown, exposure | per coin `marked_equity` combined on the union of hour closes; exposure as the union (AC-9) |
| ruin | close price per coin | each coin's last quote in the cache (AC-4b) |
| entry size | equity, open notional | the shared account's equity; `guard.open_notional` over every coin (AC-5) |
| `buy_and_hold` | per coin notional | benchmark file `notional_usd` ÷ coin count (AC-11) |
| report | buy and hold figure | mean over coins of `(last close − first close) ÷ first close` (AC-9) |
| gate | per coin net profit | `positions.instrument_id` with `realized_pnl + funding_total` per closed holdout position (AC-13) |
| gate | `instrument_max_share` | `thresholds.toml` v2, 0.60 (the same bar as every concentration check) |
| results | per coin trades and net | holdout report `per_instrument` block (AC-9) |

**Key invariants**:
- Single coin run files, studies, frozen hashes, and verdicts are unchanged (AC-3, AC-13).
- No `trading_core.node` module is added or edited (AC-1); multi coin wiring is lab only.
- `RiskLimits` and the risk check are unchanged; one account, one equity, one exposure cap (AC-5).
- The gate only gets stricter: no existing threshold moves, one check is added.
- Every grid point is a counted trial; the study file is committed before its first run.
- Money stays `Decimal`; timestamps stay UTC nanoseconds.

**Security model**: the lab is laptop only and demo only (`TRADING_ENV=live` exits 78). ETH data comes from Kraken's public endpoints and the funding export you download by hand; no key is used. Bundles still pass the importer's demo, backtest, and `BACKTESTER-LAB` checks; the new instrument rows are refused on any content conflict. The repo is public, so run reports stay git ignored under `lab-reports/`.

**Configuration required**: none new. No new settings in `/etc/kfs/kfs.env`.

**Critical test scenarios**:
- Two coin fixture run: both instances trade, rows carry the right `instrument_id`, one combined equity curve, verifies **AC-4**, **AC-9**
- Shared sizing: with the first coin's position near `max_total_exposure`, the second coin's entry is clipped by `exposure_cap`, verifies **AC-5**
- One coin gap: fixture ETH candles with a 2 hour gap block only ETH entries while BTC trades, gaps reported for ETH only, verifies **AC-6**
- Determinism and tie order: the same run twice is byte identical; equal timestamps are processed BTC after ETH (sorted ids), verifies **AC-7**
- No double rows: each order and position event is recorded once, under its own coin; ETH's slippage side ignores a BTC position, verifies **AC-4**
- Shared ruin: account equity reaches 0 with both coins open; both are closed with reason `ruin`, the variant reports ruin and does not fail, verifies **AC-4b**
- Combined marked equity: two coins with known hour marks give the expected combined curve, drawdown, and union exposure, verifies **AC-9**
- Stored v1 thresholds JSON loads under the new loader, verifies **AC-13**
- Off tick body: a close of `1660.691517005051` on a candle with high `1660.691517005051` and low `1639.7` is stored as 1660.6 (nearest is 1660.7, above the stored high 1660.6) and the next open of the same raw value inside 1658.7 to 1692 as 1660.7; a close of `1617.853075194912` inside 1603.1 to 1643.2 is stored as 1617.9, and the next open (also its low) as 1617.9; a quiet minute repeating an off tick raw close is stored flat at the previous stored close and passes the quiet check; all count in `price_rounded_rows`; a candle wholly inside one tick is a bad row; 11 off tick candles in one month are all bad rows and 10 are rounded; every committed BTC month builds byte identical rows, verifies **AC-2a**
- Funding isolation: an ETH position takes only ETH's rate at ETH's charge times, and a single coin run's samples map has one key, verifies **AC-8**
- Single coin stability: `plan_hash` and version configs for `donchian_4h.toml` and `macd_baseline.toml` match the base commit, verifies **AC-3**
- Run file refusals: both keys, neither, one item list, duplicate, unpinned id each exit 64, verifies **AC-3**, **AC-2**
- Single coin commands refuse `instrument_ids` with exit 64, verifies **AC-10**
- Benchmark split: `buy_and_hold` on two coins opens each at half the file's notional and never trades again, verifies **AC-11**
- Bundle instruments: import of a bundle with a new instrument upserts it; same content again is a no op; different content refuses the file; an old bundle loads, verifies **AC-12**
- Gate check: BTC 0.7 of profit fails `concentration_instrument`; 0.55 passes; one coin only and total ≤ 0 skip; a v1 single coin fixture gives the same verdict under v2, verifies **AC-13**
- Study detail shows coin share only when the metric exists, verifies **AC-14**
- Hash stability at close out, verifies **AC-1**, **AC-18**

## Build plan

Tracer Bullet: the thinnest real thread is two coins through one engine, on real ETH data, into one bundle the VPS gate can import. The study only runs once that thread works end to end.

1. [x] Branch from main, record the AC-1 hash baseline in `verify.md`, satisfies **AC-1**
2. [x] ETH data: the pinned `PF_ETHUSD.KRAKEN.json`, `--instrument` on `fetch-candles` and `import-funding` with tests; the off tick open and close rounding with its tests; fetch ETH candles and import its funding on the laptop; record the data quality figures, the off tick roundings, and the shared first month in `verify.md`. Commit `feat(lab): ETH candles and funding (0022)`, satisfies **AC-2**, **AC-2a**
3. [x] Thin thread: `instrument_ids` in the run file and `CandleUnitPlan`; `candle_tree` per coin; the multi coin unit (per coin instances and guards, the shared recorder and side flag with per coin maps, the merged stream, the shared ruin handler); per coin funding samples and funding pass; the single coin stability, determinism, tie order, sizing, gap, ruin, and funding tests on fixture candles. Smoke run `buy_and_hold` (a benchmark, not a trial) on BTC plus ETH over one real month, satisfies **AC-3**, **AC-4**, **AC-4b**, **AC-5**, **AC-6**, **AC-7**, **AC-8**
4. Report `per_instrument` block and the mean buy and hold figure; refusals in the single coin commands, satisfies **AC-9**, **AC-10**
5. Bundles carry instruments, the importer upserts them, with tests; push the step 3 smoke bundle to a laptop gate import to prove it loads, satisfies **AC-12**
6. Gate: `instrument_id` on `TradeReturn`, `concentration_instrument`, thresholds v2, the v1 fixture parity test; dashboard coin share. Commit `feat(gate): concentration_instrument check, thresholds v2 (0022)`, satisfies **AC-13**, **AC-14**
7. Multi coin benchmarks in `lab holdout`, with the 50/50 split test, satisfies **AC-11**
8. Checkpoint with the trader: show the thin thread results and the ETH data record, then commit the study file and rebuild `kfs/lab` with `GIT_COMMIT`, satisfies **AC-15**
9. Run `lab study` and `lab holdout`. Then, each VPS step confirmed: deploy the gate and dashboard (thresholds v2), push the bundles, run `gate evaluate donchian_4h_pair` once, satisfies **AC-16**
10. Write the Results and `verify.md` run log, commit `docs(specs): donchian_4h_pair verdict (0022)`, satisfies **AC-17**
11. Close out: hash comparison again, scope tick, paper follow up only on a pass, satisfies **AC-18**

## Consequences

**Positive**:
- About twice the trades on the same rule, which is what `donchian_4h`'s failed checks (bootstrap, deflated Sharpe, top trades share) most need.
- The lab can run any registered candle rule on several coins, which later portfolio ideas, more coins, and a live multi coin account reuse.
- One shared account matches how Kraken margin really works, so exposure and sizing interplay is tested, not assumed.
- The gate only gets stricter, and old verdicts stay valid.

**Negative / tradeoffs**:
- BTC's holdout was already seen in spec 0020 (`donchian_4h` and its revision), and the rule was picked because it did best there. Only the ETH half is fresh, so a pass is weaker evidence than its numbers suggest; it is labelled "BTC holdout seen".
- BTC and ETH are strongly correlated (they often move together), so the diversification is smaller than two coins suggest, and `concentration_fold` and `concentration_top_trades` may still fail.
- Liquidation is an approximation. Each coin's `LiquidationGuard` (frozen lab code, spec 0011) works out its liquidation price from equity at entry and ignores the other coin's open P&L and notional, while Kraken liquidates on the whole account's margin. A coin can therefore look safe while the account is not. The account level backstop is the shared ruin at equity ≤ 0 (AC-4b), and the per coin `min_liq_distance_pct` 10% stays a conservative entry filter. With 0.5% risk per trade and 2x leverage the gap should rarely matter, but a cross margin guard is owed before multi coin live trading.
- 4 more trials raise the `hour_level` candle family N (27 to 31 if nothing else runs first), which lowers every member's deflated Sharpe.
- The study takes about twice the BTC study's wall time and catalog disk for ETH's minute candles.
- A bundle format change and a thresholds version bump both need the VPS deployed before the push.

**Neutral**:
- `packages/core/AGENTS.md` and `apps/lab/AGENTS.md` should mention `instrument_ids` and `concentration_instrument` once they land (a `/sync` item).
- The trader stays single strategy, single coin; nothing here changes paper or live trading.

## Follow-up

- [x] `/scope`: enroll feature 25 "Multi coin portfolio" linking this spec, and drop the deferred "ETH candles" row (done by `/architect`, 2026-10-28).
- [ ] Before multi coin live trading: a cross margin liquidation guard (AC-4b and the liquidation tradeoff above).
- [ ] `/sync`: `apps/lab/AGENTS.md` (multi coin run files, `--instrument`), `packages/core/AGENTS.md` (`concentration_instrument`, thresholds v2).
- [ ] On a `pass`: `/scope` the paper feature that runs a registered strategy on several coins in the trader (joins the deferred "more than one strategy in the paper trader" row).
- [ ] On a fail: more coins (the deferred "More instruments" row, with a liquidity filter and a fresh holdout on coins never studied) is the next honest step before any more BTC only rules.

## Results

Not run yet.
