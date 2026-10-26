# 0019.2 EMA pullback (`ema_pullback`)

## Summary

Trade with a short term trend. When the 8 bar EMA (exponential moving average) is above the 21 bar EMA, wait for price to dip to the 8 EMA with RSI(7) cooling to 30 to 50, then buy when a bar closes back above the 8 EMA. The stop sits past the dip's low or the 21 EMA, the target is 1.5 or 2 times the risk, and the trade lasts at most 8 hours. Shorts are the mirror.

## Hypothesis

- **Why it might work:** intraday BTC trends move in legs; a shallow pullback that holds the 21 EMA is often a pause before the next leg, and a target of 1.5 to 2 R at 15 minute scale is far above the 14 bps cost.
- **When it should work:** trending sessions with clean legs (US hours on news free trend days).
- **When it should fail:** choppy ranges, where the EMAs cross back and forth and each pullback is a reversal; and whipsaw around news.
- **No trade:** during warmup (the first decision is possible at the close of bar 21, when EMA(21) is ready; RSI(7) and ATR(14) are ready sooner); when the EMAs are equal.

## Rules

Indicators on bar closes: `indicators.Ema(fast)`, `indicators.Ema(slow)`, `bar_indicators.Rsi(rsi_period)`, plus the base's ATR from `update`. Ready when all have a value and ATR is ready.

At each bar, in this order:
1. **Trend:** up when `EMA(fast) > EMA(slow)`, down when below, none when equal.
2. **Disarm:** if armed from an earlier bar, first add 1 to `armed_bars`. Then an armed long is disarmed when the trend is not up, or the close is below `EMA(slow)`, or `armed_bars > arm_bars` (the bar it armed on counts as 1, so with `arm_bars` 6 the last bar that can trigger is the sixth). The mirror for an armed short.
3. **Arm:** when not armed (including a bar that was just disarmed in step 2, which can re-arm at once with fresh `pullback_extreme` and `armed_bars` = 1), in an up trend, a bar whose low ≤ `EMA(fast)` and whose RSI is at least `rsi_low` and at most `rsi_high` arms a long, with `pullback_extreme` = that bar's low. The mirror for a short (high ≥ `EMA(fast)`, RSI at least `100 − rsi_high` and at most `100 − rsi_low`, `pullback_extreme` = the bar's high).
4. **Track:** while armed (including the arming bar), `pullback_extreme` = the lowest low (long) or highest high (short) since arming.
5. **Long signal:** armed long and `close > EMA(fast)`. It may fire on the arming bar itself. The RSI band is checked only on the arming bar, not on the trigger bar. Reason `ema_pullback_long`. **Short signal** mirrored, reason `ema_pullback_short`.

- **Structural stop:** long `min(pullback_extreme, EMA(slow))`, short `max(pullback_extreme, EMA(slow))`. The base's 1 ATR floor then applies.
- **Exit:** long when `close ≥ r_target(trade, target_r)`, short when `close ≤ r_target(trade, target_r)`. Reason `target`.
- **Time stop:** `time_stop_bars` = 32 (8 hours).
- **`values`:** `ema_fast`, `ema_slow`, `rsi`, `trend` (`up`, `down`, `none`), `armed` (`long`, `short`, `none`), `pullback_extreme` (or `None`), `armed_bars`.

## Setup state

`armed` (side or none), `pullback_extreme`, `armed_bars`. Steps 1 to 4 run in `update` on every bar, also in a position; `entry()` is a pure read of step 5. `consumed()` disarms (clears all three). `on_flat()` does nothing. The spec 0018 shared rule (a trigger while in a position is dropped) needs no code here: the base already calls `entry()` and then `consumed()` while not flat, so a trigger in a position disarms. An arm that has not triggered by the end of a trade can still fire after it.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"15-MINUTE-LAST"` | no | must be `15-MINUTE-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `fast` | 8 | no | ≥ 2 and < `slow` |
| `slow` | 21 | no | > `fast` |
| `rsi_period` | 7 | no | ≥ 2 |
| `rsi_low` | 30 | no | > 0 and < `rsi_high` |
| `arm_bars` | 6 | no | ≥ 1 |
| `time_stop_bars` | 32 | no | base |
| `target_r` | | `["1.5", "2"]` | > 0 |
| `rsi_high` | | `[45, 50]` | > `rsi_low` and ≤ 50 |

4 grid points. Config defaults for the grid fields are the grid's first point.

## Tests (AC-6)

- An up trend fixture where a bar dips below EMA(8) with RSI(7) 40 and the next bar closes above EMA(8) gives one long at that close, stop at the lower of the dip low and EMA(21).
- A dip bar that closes back above EMA(8) gives the long on that same bar.
- The mirror fixture gives a short.
- RSI(7) at 55 on the dip bar does not arm.
- A close below EMA(21) while armed disarms; a later close above EMA(8) gives no signal.
- An arm whose first close above EMA(8) is on its sixth bar gives a signal; one whose first close above is on its seventh bar gives none.
- On the seventh bar of a stale arm, a bar that is itself a valid dip bar re-arms, with its own low as `pullback_extreme` and `armed_bars` 1.
- A trigger while in a position is dropped: after the trade closes, the same arm gives no signal.
- A long whose close reaches 2 R exits with `target`; one open after 32 bars exits with `time_stop`.
- No decision before the close of bar 21; the first decision is possible at bar 21.
- `fast = 21, slow = 8` and `bar_spec = "1-HOUR-LAST"` are refused by the config.

## Expected and risks

Likely 200 to 600 trades a year. The ATR floor will often widen the stop past the structural one on shallow dips, which moves the R target out too. Watch `concentration_direction` (BTC's up years favour longs) and the walk forward Sharpe in the 2022 bear folds.

## Short rationale

Your feature 16 notes carried this rule at 1 minute, where its 1.5 to 2 R target could not clear the fees. At 15 minutes the same R target is several times the cost. It has few settings and decides only on closes.

## Results

**Verdict: `fail`.**

| Field | Value |
|---|---|
| Study | `ema_pullback` (study file committed 2026-10-24 12:03 UTC in 24216d9, study created 2026-10-25 19:55 UTC) |
| Promotion id | `01a1766f-6593-7011-b417-3bb821acea4b` (VPS) |
| Evaluation date | 2026-10-26 |
| Chosen grid point | 1 of 0 to 3 (`target_r` 2, `rsi_high` 45), chosen in all 5 folds |
| Walk forward Sharpe | −8.214422 (5 folds, every fold negative; return −96.15%) |
| Holdout Sharpe | −8.133190, against buy and hold −0.460362 and the MACD baseline −1.377988 |
| Holdout return after costs | −73.91% |
| Holdout marked max drawdown | 74.21% |
| Deflated Sharpe probability | 0.000000 |
| PBO | 0.015873 |
| Holdout trades | 706 (344 long, 362 short; 149 exits at `target`, 79 at `time_stop`, 478 stopped out; win rate 26.2%) |
| Trial count N, trial pool | 16, 16 (`kind+lab`, VPS) |
| `avg_hold_minutes` | 164.4 (longest 480) |
| Holdout gross per trade | −4.834 USD |
| Holdout cost per trade | 5.642 USD |

Reasons (code, value, threshold):
- `fee_hurdle`: `failed`, −4.834, 8.463
- `holdout_sharpe`: `failed`, −8.133190, 0
- `beats_buy_and_hold`: `failed`, −8.133190, −0.460362
- `beats_macd_baseline`: `failed`, −8.133190, −1.377988
- `walk_forward_sharpe`: `failed`, −8.214422, 0
- `bootstrap_lower_bound`: `failed`, −0.004401, 0
- `deflated_sharpe`: `failed`, 0.000000, 0.95
- `drawdown_budget`: `failed`, 0.742, 0.10

Reading: the trade rate was high (about 700 a year, 323 to 375 per half year fold), so the 15 minute pullback trades often enough. But the edge before costs is negative: gross per trade is −4.8 USD, so the loss is not only the cost. A pullback to EMA(8) inside an EMA(8/21) trend is followed by a stop more often than a 2 R move (26% winners, while a 2 R target needs about 33% to break even before costs). All 4 grid points lost about 99% over the full study run (−99.10% to −99.96%). No revision (AC-13).
