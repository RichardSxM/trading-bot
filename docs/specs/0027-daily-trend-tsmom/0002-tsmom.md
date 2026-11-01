# 0027.2. `tsmom`: daily momentum, long only, on BTC, ETH, SOL and XRP

## Summary

`tsmom` holds a coin long while its daily close is above its close a set number of days ago, and is flat otherwise. It never shorts. It trades with market orders at the 00:00 UTC daily close, and has a wide 3 ATR protective stop in case the market crashes within a day. This child adds the rule, SOL and XRP data, and one four coin study with a 4 point lookback grid, run through the gate once. The verdict is written here, pass or fail.

## Hypothesis

Crypto trends persist for weeks. Being long only while the 1 to 2 month return is positive captures most of the big rises and sits out most of the big falls, by far more than the costs. Pre check on development data, before this spec (no stop, 0.5 % risk on 2 ATR): 61.8 trades a year pooled, gross per trade +492 and +417 bps in the two halves, max drawdown 7.7 %, daily Sharpe 1.29 a year, all four coins profitable. Expected failure modes: the 60 trade floor in a quiet year, a drawdown above 10 % in a choppy year, losing to buy and hold in a strong bull year, and the bootstrap lower bound, since the profit sits in few trades.

## Requirements

- **AC-12** (rule): pure `trading_core/strategy/tsmom.py`, `TsmomRule(BarRule)`, with `lookback_days: int` (at least 1) and `stop_atr: Decimal` (above 0).
  - It keeps the last `lookback_days + 1` closes and is ready once it holds them and the base gives an ATR. `warmup_bars()` is `lookback_days + atr_period + 1` (child 1 AC-10a).
  - `entry()` is only the level condition: when the close is strictly above the close `lookback_days` bars before it, it returns `EnterSignal(LONG, stop = close − stop_atr × ATR, reason "tsmom_long")`, else nothing. It never returns a short. The rule holds no position state: the base calls `entry()` only when flat (and drops the signal otherwise), and calls `exit()` only when a position is open.
  - `exit()`: when the close is at or below the close `lookback_days` bars before it, it returns `"momentum_exit"`.
  - `consumed()` and `on_flat()` do nothing, since there is no stored setup to clear.
  - The signal is a level, not a stored setup. After any exit (stop, momentum, time stop, range end), a later close that meets the entry condition enters again, including the very next close. The base's `fresh_signal` may skip a stale entry, which is harmless (the next close re-enters). A `momentum_exit` is never skipped as stale.
  - `values()`: `close`, `close_lag`, `momentum` (`close / close_lag − 1`), `lookback_days`.
  - The module is frozen after the first study (spec 0018 AC-7).
- **AC-13** (node and config): `trading_core/node/tsmom.py`, with `Tsmom(BarRuleStrategy)` building `TsmomRule`, and `TsmomConfig(BarRuleConfig)`.
  - `TsmomConfig` adds `lookback_days: int = 20`, `stop_atr: Decimal`, and `long_only: bool = True`. The default exists because `check_strategy` validates `[strategy.config]` before a grid point is applied; it is the grid's first point.
  - Refusals (`ValueError` at config build): `long_only` False (the strategy has no short side, and the flag is the gate's declaration, child 1 AC-6), `bar_spec` other than `1-DAY-LAST`, `lookback_days < 1`, `stop_atr <= 0`.
  - Entries and exits are the base's market orders on the bar close. The stop is the base's floored, risk sized protective stop.
  - The frozen version config therefore always holds `long_only: true`.
- **AC-14** (registry): the lab registry gains `tsmom` with `kind=StrategyKind.TREND`, `module="trading_core.node.tsmom"`, `candle_screen=False`, `candle_gate=False`, `run_fields=_candle_fields`. The trader has no `tsmom` entry until the paper follow up, so no `trader.version` output is expected here; `check_version` is tested with a hand made `trend` payload (child 1 AC-11).
- **AC-15** (SOL and XRP pins): `apps/lab/src/lab/instruments/PF_SOLUSD.KRAKEN.json` and `PF_XRPUSD.KRAKEN.json` have the fields and format of `PF_ETHUSD.KRAKEN.json`.
  - Source, in order: the VPS catalog's newest instrument row for the coin, if the recorder has one (the ETH method, spec 0022 AC-2). Otherwise Kraken Futures' public instruments endpoint (`/derivatives/api/v3/instruments`): `tickSize` gives the tick size, `contractSize` the multiplier, `contractValueTradePrecision` the size step, and the first entry of `marginLevels` (the tier from size 0) gives `initialMargin` and `maintenanceMargin` for the ETH file's margin fields.
  - `maker_fee` `0.0002` and `taker_fee` `0.0005` are written in by hand, with a `_note` saying the source and the date read.
  - If neither source gives a field the pin needs, stop and come back to `/architect` (no guessed value).
  - **XRP tick (amended 2026-11-01)**: `PF_XRPUSD.KRAKEN.json` pins `price_increment` `"0.00001"` and `price_precision` `5`, Kraken's historical tick, not today's `0.0001`. Reason: Kraken traded PF_XRPUSD on 0.00001 until about 2026-08-20, so every XRP month from 2022-03 to 2026-08 has opens and closes off today's tick and fails AC-16 (verify.md). Today's prices are multiples of 0.0001, so they sit on the finer tick too. Every other XRP field stays as read from the endpoint. The `_note` says the tick comes from Kraken's own candles, not the endpoint, and gives the change date.
  - The finer tick is a lab pin only. It changes stop and fill rounding by at most 0.0001 (under 4 bps at XRP's lowest price in the data), small next to the 14 bps cost and the pre check's ~300 bps gross. The paper follow up trades the venue's live instrument (0.0001), not this file.
  - Before the XRP refetch, the lab catalog holds no XRP instrument or candle row at the old tick (today only the funding tree exists). If one exists, it is removed first, so two precisions never mix in one tree.
  - If an XRP month still fails `lab_candles_bad_rows` at 0.00001, stop and come back to `/architect` (no further tick change, no relaxed threshold).
- **AC-16** (SOL and XRP data): `lab fetch-candles --instrument` and `lab import-funding --instrument` run for both coins on the laptop, from the first minute Kraken has through the last complete UTC day before the run (the holdout needs data to its end, not only to 2025-10-18). The study's holdout ends at `last_complete_day`, the earliest last day over the four coins, so `verify.md` records each coin's last day and flags one more than 2 days behind the others. The data passes the checks BTC and ETH pass (spec 0014 AC-1 to AC-3, spec 0022 AC-2 and AC-2a, the off tick rounding included). `verify.md` records per coin the first candle minute, the first whole month, per year the missing minutes, gap rows, and funding coverage. If Kraken's funding export holds no SOL or XRP history before 2025-10, the study does not run: stop and come back to `/architect`.
- **AC-17** (four coin unit and daily bars): a candle run file with four `instrument_ids` runs as spec 0022's multi coin unit (one engine, one margin account, sizing over every open position, shared ruin, per coin gaps and funding). The base aggregates `1-DAY-LAST` bars from the lab's 1 minute candles. The bar stamped 00:00 UTC on day D holds only the minute candles that opened on day D − 1 (00:00 to 23:59). The 00:00 candle of day D belongs to the next bar, so the decision at the close can't see a later price. A fixture test (synthetic candles for four coins) proves:
  - each daily bar's close equals the last minute candle of the day before, and no minute after the bar's timestamp is in it (the `--lookahead-check` of `lab backtest`, run on the fixture only)
  - a long entry on the first close above the lagged close
  - a `momentum_exit` on the first close at or below it
  - a stop exit, then a re-entry on the next qualifying close
  - no short order ever
  - per coin positions in one account
- **AC-18** (study file): `apps/lab/studies/tsmom.toml`:
  - `instrument_ids = ["PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN", "PF_XBTUSD.KRAKEN", "PF_XRPUSD.KRAKEN"]`
  - `from` is the first whole month all four have candles (2022-04-01 unless AC-16 finds a later one), `to = 2025-10-18`
  - `seed = 7`, `stale_quote_s = 300`, fees `0.0002` and `0.0005`, `slippage_bps = "2"`, `starting_balance_usd = "10000"`, `leverage = "2"`
  - the `[risk]` block of `donchian_4h_pair.toml`, value for value
  - `[strategy] name = "tsmom"`
  - `[strategy.config]`: `bar_spec = "1-DAY-LAST"`, `atr_period = 20`, `atr_floor_mult = "1"`, `time_stop_bars = 30`, `stop_atr = "3"`, `long_only = true` (written explicitly, since the gate reads it from the version config, child 1 AC-6)
  - `[strategy.grid] lookback_days = [20, 30, 40, 60]`
  - `[study] name = "tsmom"`, `lab = "candle"`, `seed = 42`

  Why 30 bars: the risk check budgets funding for every hourly boundary inside the time stop (`funding_periods`), so 365 bars would budget 8,760 periods (about 11 % of notional at 3 bps a day) and halve every position. 30 bars budgets about 1 %. A trade that hits the time stop and still qualifies re-enters at the next close, for one round trip of costs.

  Its header comment names this spec, says the study holds out once, and says the plan is frozen by name. It is committed, and the lab image rebuilt with `GIT_COMMIT`, before its first run.
- **AC-18a** (pre check rerun as built): before the study file is committed, the session pre check script is rerun on development data only (2022-04-01 to 2025-10-18, the four coins, no holdout day), with the build's rules: the 3 ATR(20) stop, the 30 bar time stop with re-entry, risk sized at 0.5 % of equity over the stop distance, capped by the `[risk]` block, and funding budgeted as `funding_periods` does. `verify.md` records per lookback (20, 30, 40, 60) trades a year, gross per trade by half, max drawdown, and Sharpe, next to the first pre check. If lookback 30 no longer clears the first pre check's bars, the study does not run: stop and come back to `/architect`. This rerun is not a database trial and changes no value in this spec.
- **AC-19** (run and verdict): on the laptop, `lab study studies/tsmom.toml`, then `lab holdout tsmom` once (benchmarks: `buy_and_hold` on the four coins, child 1 AC-8).
  - The grid point is picked as for every study (best walk forward development Sharpe, the gate's existing rule). A winner that then fails any check is a `fail`. There is no second pick, even when the winner is a lookback (40 or 60) that missed the first pre check. Then `scripts/push-bundles.sh`, and on the VPS with `--no-deps`, `gate import` and `gate evaluate tsmom`, once. `verify.md` logs each command with its time (UTC and Amsterdam), duration, exit code, and the batch, study, and promotion ids. A second evaluate by mistake is logged, and the first promotion stays the one of record.
- **AC-20** (results record): a `## Results` section below gets, in its own commit `docs(specs): tsmom verdict (0027)`:
  - the fields of spec 0022 AC-17: study, promotion id, evaluation date, verdict, every failed reason with value and threshold, walk forward Sharpe, holdout Sharpe against buy and hold, holdout return after costs, max drawdown, the deflated Sharpe probability, PBO, holdout trades, N (with `prior_trials` 13), gross and cost per trade, and per coin holdout trades, net P&L, and coin share
  - the chosen grid point
  - the not applied trend checks with their recorded metrics (`direction_share`, `top_trades_share`)
  - the label "BTC and ETH holdout seen (specs 0020, 0022); trend rules set after the development pre check"

  A fail is recorded the same way and never removed.
- **AC-21** (close out): after the verdict, the AC-1 hash comparison runs again and the scope feature's Build it box is ticked. On a `pass`, the paper follow up is enrolled (index Follow-up). No revision of `tsmom` runs in this spec.
- **AC-22** (no extra looks): no command reads `tsmom` results on real candles before the study file's commit, and `lab backtest` is never run on real candles for `tsmom`. The only holdout look is AC-19's single `lab holdout tsmom`. The database enforces one completed holdout per study (`uq_study_runs_study_id`), but a holdout that crashed can be run again even though it read holdout data. If that happens, `verify.md` logs the crash, its time, and how far it got, and the rerun's verdict carries that note in the Results.

## Design

**State transitions**: per coin, the `BarRuleStrategy` cycle unchanged: flat → entry (taker at the daily close) → open with a stop → closed by the stop, `momentum_exit`, the time stop (30 bars), or the range end → flat. The next qualifying close re-enters. Each segment starts after the pre roll (child 1 AC-10a), so the rule can be ready on the segment's first close.

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| entry | the lagged close | the rule's own close window (AC-12) |
| entry | stop price | `close − stop_atr × ATR(20)` from the rule, floored by the base at `atr_floor_mult` ATR from the quote mid |
| entry | quantity | the base's `check_entry`: `risk_per_trade` 0.005 of shared equity over the stop distance, capped by the `[risk]` block |
| exit | `momentum_exit` | the rule's `exit()` on the bar close |
| daily bar | OHLC | the base's `1-DAY-LAST` internal aggregation of the 1 minute candles |
| funding | per coin rate | `lab import-funding` samples per coin (spec 0022 AC-8) |
| entry | funding budget | `funding_periods` over the 30 bar time stop, at the coin's rate at entry |
| pins | tick, size step, margins | the VPS catalog row, else Kraken public instruments endpoint with the AC-15 field mapping; fees by hand; XRP tick 0.00001 from Kraken's historical candles (AC-15 amendment) |
| study | `from` | AC-16's first whole month common to all four coins |
| verdict | every metric | `gate evaluate`'s promotion row (AC-19) |

**Key invariants**:
- `tsmom` never sends a short entry. A short position in any run is a bug, and the gate fails it (`long_only_violated`).
- One account for all four coins. Sizing always sees every open position.
- The grid is 4 points. Every point is a trial, and N = 13 + every distinct frozen hash in the trend family.

**Critical test scenarios**:
- Rule on synthetic closes: entry exactly when close > lagged close, not when equal. Exit when close ≤ lagged close. No short ever. Re-entry right after a stop. The stop is 3 ATR under the close. Verifies **AC-12**.
- Config refuses `long_only=False`, a `4-HOUR-LAST` bar spec, a lookback of 0, and a stop of 0. Verifies **AC-13**.
- `tsmom` registry entry has kind `trend`, and the study file's `[strategy.config]` alone passes `check_strategy` (the `lookback_days` default). Verifies **AC-13**, **AC-14**.
- At a funding rate of 3 bps a day and the 30 bar time stop, a fixture entry is sized (not refused with `zero_quantity`). Verifies **AC-18**.
- The pinned SOL and XRP files load, and an unpinned id still exits 64. The XRP pin loads with tick 0.00001 and precision 5, and a price on today's 0.0001 tick counts as on tick. Verifies **AC-15**, **AC-16**.
- The four coin fixture study runs end to end, with per coin positions in one account. Verifies **AC-17**.

## Results

_Filled in by AC-20 after the run._

## Rationale (short)

Momentum on the 30 day return was the one long only rule among 10 pre registered trials that cleared every pre check bar. Its neighbours (20, 40, 60 days) behaved the same, so the grid holds them instead of one lucky value. Long only, because every long and short variant had drawdowns of 12 to 22 %. Market orders at the close, because 14 bps a round trip is small against moves of hundreds of bps and a missed fill costs more. A 3 ATR stop, because the base and the risk rules need one, and a wide one leaves the momentum exit in charge. A 30 bar time stop, because the risk check budgets funding over the whole planned hold. The stop and the time stop were not in the first pre check, so AC-18a reruns it as built before anything is committed. Full options in [rationale.md](rationale.md).
