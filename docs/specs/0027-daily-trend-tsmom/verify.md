# Verify: daily trend candidate (tsmom) · spec 0027

## AC-1 hash baseline (2026-11-01)

The branch `feat/0027-daily-trend-tsmom` starts from origin/main 0be15e1 (its only commit on top, 753ec82, adds this spec and no code).

- **Node modules**: `code_hash(code_closure(m, root))` for every `trading_core.node` module (40) on 0be15e1, run through `scripts/spikes/hash_check.py` in `kfs/lab:s0026` against a `git archive` of 0be15e1's `packages/core/src`. The sorted `module hash` list (one line per module) has sha256 `8a8f462227ec429275e91e7ee726f7f1a4d1cceb94defdd54e77413948513ead`. The close out (AC-21) runs the same list on the merged code and names every changed line with its reason.
- **`donchian_scalp_r2` on demo**: `python -m trader.version` on the VPS (git commit 0be15e1, `PAPER_STRATEGY=donchian_scalp_r2`, instruments PF_PEPEUSD and PF_XBTUSD, kind `scalping`) gives `code_hash` `dc96df87a2bab7c152e7d7d1184e1d72aa88784c954d757154598e90c08eced2` and `frozen_hash` `a1ad9ba1948e050e4c6a3d668e577e21b1f37cd927915f95750be9b5b7cfce10`. AC-3 changes `recording/enums.py`, so both are expected to change, and the deploy needs your manual promote.

## AC-2 to AC-8 and AC-11, kind and gate (2026-11-01)

- **AC-2, AC-3**: migration `0014_trend_kind` (`20261101_0014_trend_kind.py`) and `StrategyKind.TREND`. `tests/integration/test_trend_kind_migration.py`: the upgrade keeps scalping and hour level rows, then accepts `trend` and refuses `other`; the downgrade with no trend row restores the old check text exactly; with a trend row it refuses (`RuntimeError`) and leaves 0014 in place. The round trip and autogenerate drift tests pass unchanged (the model's check follows the enum through `_one_of`).
- **AC-4**: `thresholds.toml` version 3 with `[trend] prior_trials = 13`. A stored v1 and v2 JSON round trips with `json.dumps` byte identical (no `trend` key), 0 loads, −1 and 1.5 are refused.
- **AC-5**: a trend study of 4 hashes gets N = 13 + the family count, its trial Sharpes stay database only, and an hour level study's N and `prior_trials` (0) are unchanged (`test_family_trials.py`).
- **AC-6 to AC-8**: `test_gate_verdict.py` trend cases: long only all long passes with `direction_applicable` false and `direction_share` 1.000000; one short trade gives `long_only_violated` 1; no measures gives `missing_metric`; trend without `long_only` and hour level (either flag) keep `concentration_direction`; top 5 % share 1.4 is recorded with no reason for trend and fails scalping; trend is judged against buy and hold only.
- **AC-11**: `gate check-version` parses and writes a hand made `trend` payload (`apps/gate/tests/test_check_version.py`).
- Suites: core unit 2267 passed, core integration 185 passed, gate 38 passed; ruff and mypy strict clean.
- Not changed, by AC-3: `PaperThresholds.trade_floor` gives a non hour level kind the scalping floor (100). No paper step runs for `trend` before the paper follow up, which owns the trend paper floor.

## Thin thread: AC-10, AC-10a, AC-12 to AC-15, AC-17 (2026-11-01)

- **AC-12, AC-13**: `trading_core/strategy/tsmom.py` (`TsmomRule`, `warmup_bars`) and `trading_core/node/tsmom.py` (`Tsmom`, `TsmomConfig`). `test_strategy_tsmom.py`: entry only on a close strictly above the lagged close, the stop 3 ATR under the close, `momentum_exit` at or below, never a short, re-entry on the next close after an exit, warmup 51 for lookback 30. `test_node_tsmom.py`: daily internal bars; `long_only` false, `4-HOUR-LAST`, lookback 0, and stop 0 are refused.
- **AC-14, AC-10**: registry entry `tsmom`, kind `trend`, module `trading_core.node.tsmom`, no candle screen or candle gate. The run file check admits `trend`; a run file with `long_only = false` is refused (exit 64 path); `lab screen` refuses the trend study (`is not a candle study of a scalping strategy`, exit 64).
- **AC-15**: `PF_SOLUSD.KRAKEN.json` and `PF_XRPUSD.KRAKEN.json` from Kraken's public instruments endpoint, read 2026-10-31 23:50 UTC (2026-11-01 00:50 Amsterdam); the VPS recorder has no SOL or XRP row. SOL: tick 0.01, size step 0.01 (`contractValueTradePrecision` 2), margins 0.01 and 0.005. XRP: tick 0.0001, size step 1 (precision 0), margins 0.02 and 0.01. Check of the mapping: the endpoint's ETH row gives tick 0.1, precision 3, margins 0.01 and 0.005, exactly the pinned ETH file. Two older tests used `PF_SOLUSD` as their unpinned example; they now use `PF_DOGEUSD`.
- **AC-10a as built**: a candle study is one development run plus one holdout run (its folds are cut from the run's daily equity), so the pre roll applies at the development start and at the holdout start. For a strategy of kind `trend`, `run_candle_unit` reads each coin's candles from `pre_roll_start` (`trading_core/lab/pre_roll.py`: `warmup_bars()` bars before the segment start, never before the coin's first stored month), starts the engine there, and calls the base's `signals_after(segment start)`, so every entry signal from a pre roll bar is dropped with no decision. The segment's own facts start at the segment start: funding samples and charges, the equity curve and measures, `first_trade_ns`, and zero volume bars. After the run, any decision, order event, fill, or position before the start fails the variant (`_keep_segment_rows`, fail closed), and pre roll risk events (a `stale_feed` over a candle gap) are dropped. Other kinds keep the cold start. The `buy_and_hold` benchmark is kind `hour_level`, so it keeps its cold start: the same result as a pre roll it ignores.
- **AC-10**: the plan's thresholds version is the study row's frozen `thresholds` JSON (`version` 3); no plan key was added, so no other kind's `plan_hash` moves.
- **AC-17**: `apps/lab/tests/test_tsmom.py`, four hand built coins in one account, segment from day 5 with a 5 day pre roll: each first entry is on the bar stamped at the segment start with the day before's close (so no later minute is in the bar), ETH `momentum_exit` on day 8's bar, SOL stopped out by a crash then re-entered on the next close, XRP no short in a falling market, BTC no entry on equal closes, and no decision, order event, fill, position, risk event, or funding sample before the start. The look ahead prefix check (spec 0014 AC-19 method, 3 cuts) passes for `tsmom` with a pre roll.
- **Suites (2026-11-01)**: `test_tsmom_study.py` (four coin fixture study through `lab study`, `lab holdout`, and `evaluate_candidate`) 1 passed; lab unit suite 344 passed; the other lab postgres tests 5 passed; core unit suite 2284 passed and 1 failed, the known flaky `test_recording_writer.py` overflow test, which passed when rerun alone (9 passed); mypy strict and pre-commit clean.

## AC-16 SOL and XRP data (2026-11-01)

`lab fetch-candles 2022-03 2026-10 --instrument <coin>` then `lab import-funding tardis/kraken-funding-export.zip --instrument <coin>` on the laptop (`--no-deps`, the lab image of 3d946ad), 01:39 to 02:03 UTC (02:39 to 03:03 Amsterdam). The export holds both coins from 2022-03-22 16:00 to 2026-02-01 00:00 (30,431 rows each), so the AC-16 stop condition does not apply.

**SOL**: exit 0, both commands.

- **First candle minute**: 2022-03-23 11:11 UTC (BTC 10:04, ETH 10:05). The one gap row is the time before listing (32,351 minutes).
- **First whole month**: 2022-04, the same as BTC and ETH.
- **Last day**: 2026-10 stored complete through 2026-10-31.
- **Per year** (missing minutes / gap rows / missing funding charges): 2022: 32,351 / 1 / 0 (all before listing); 2023: 0 / 0 / 0; 2024: 0 / 0 / 0; 2025: 0 / 0 / 7; 2026 to date: 0 / 0 / 3. The missing charges are the same months and counts as BTC and ETH (2025-07 1, 2025-08 1, 2025-11 4, 2025-12 1, 2026-02 2), plus 1 in 2026-10, the newest charge, from the API.
- **`margin_maint`**: the pin's 0.005 is the first tier from the instruments endpoint (AC-15).
- **Off tick roundings**: no month had a bad row, so every off tick open or close stayed at or under `OFF_TICK_BODY_MAX`. Kraken's SOL daily candles have every open and close on the 0.01 tick. `price_rounded_rows` per year: 2022 1617 (most in 2022-06 to 2022-08, about 480 a month), 2023 608 (576 in 2023-11), 2024 13, 2025 and 2026 none. The fetch log of the early months is not kept, so these rows aren't split into noise snaps and real high and low roundings here.
- **BTC and ETH unchanged**: only files under the `PF_SOLUSD.KRAKEN` and `PF_XRPUSD.KRAKEN` trees were written.

**XRP: fails AC-16.** `fetch-candles` exited 1 on its first month, 2022-03, with `lab_candles_bad_rows`: 20,918 rows, 10,434 opens and 10,434 closes off the pinned 0.0001 tick, plus 50 quiet rows that follow from them. Kraken traded PF_XRPUSD on a 0.00001 tick until about 2026-08-20 and on 0.0001 since (the pin reads today's tick). In Kraken's XRP daily candles, every month from 2022-03 to 2026-08 has opens or closes on the old tick only. So with today's pin no XRP month before 2026-08 can pass the checks, and AC-15 as written can't give AC-16 clean data. The XRP funding import itself passed (exit 0, 28,188 rows added, 2,243 already present from the fetch's API rows). You chose to amend AC-15 to pin XRP at its historical tick 0.00001 (today's 0.0001 prices also sit on it), through `/architect`. The XRP fetch and the common first whole month wait for that amendment.

## AC-18a pre check rerun as built (2026-11-01)

Development data only: 2022-04-01 to 2025-10-18, no holdout day read. The four coins share one 10,000 USD account. The halves split at 2024-01-01. The daily candles are the first pre check's own (Kraken charts API, a bar's close at its day's end). Funding is every rate in Kraken's funding export at its charge time; the 4 h era rate is charged once per 4 h. The script is `lab-reports/scratch/s0027_tsmom_asbuilt.py` (git ignored, next to a copy of the first pre check, `s0027_daily_trend_precheck.py`).

It uses the build's rules:
- entry when flat at the first close above the lagged close, at the close
- stop at close minus 3 ATR(20) (the 1 ATR floor never binds), filled at the lower of the open and the stop
- a time stop on the 30th close held, otherwise a `momentum_exit`; no entry on the bar that exited at its close, re-entry on the next qualifying close, and entry at the close of a day whose stop hit inside the day
- size: 0.5 % of marked equity over the loss per unit (distance, 5 bps stop slippage, both taker fees, and 720 hourly periods at the latest stored rate, as `funding_periods` budgets)
- the `[risk]` caps (20,000 order notional and total exposure, leverage 2) and the liquidation checks (at least 10 % away and 3 times the stop distance)
- costs of 7 bps a side

Gross is the price move minus funding paid, in bps of entry, before fees and slippage. The first pre check's bars: gross at least 21 bps in both halves, max drawdown at most 10 %, a profit in both halves, at least 60 trades a year.

| lookback | as built: trades/yr | gross/trade by half (bps) | max DD | Sharpe | verdict | first pre check: trades/yr | gross by half | max DD | Sharpe | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | 89.2 | +156 / +238 | 6.2 % | 1.18 | pass | 83.3 | +223 / +275 | 10.0 % | 1.02 | pass |
| **30** | **72.3** | **+297 / +280** | **5.5 %** | **1.30** | **pass** | 61.8 | +492 / +417 | 7.7 % | 1.29 | pass |
| 40 | 65.6 | +264 / +258 | 7.1 % | 1.08 | pass | 52.7 | +541 / +451 | 9.8 % | 1.16 | fail (trades) |
| 60 | 65.6 | +223 / +256 | 5.8 % | 1.08 | pass | 50.0 | +426 / +423 | 11.6 % | 0.95 | fail (trades, DD) |

Lookback 30 still clears every bar, so the study goes ahead. These are the exits at lookback 30, with no entry refused by a cap, the liquidation checks, or stale funding:

| exit | count |
|---|---|
| `momentum_exit` | 195 |
| time stop | 43 |
| stop | 19 |

The time stop splits long trends into 30 bar pieces. That raises the trade count, but it also lowers the gross per trade and adds a round trip of costs at each re-entry. The 3 ATR stop also cuts the drawdown. The halves' P&L (lookback 30: +1,298 and +1,851 USD) is in USD on the compounding account. This rerun is not a database trial and changes no value in the spec.
