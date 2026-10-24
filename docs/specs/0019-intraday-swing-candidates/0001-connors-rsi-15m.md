# 0019.1 Connors short RSI at 15 minutes (`connors_rsi`, study `connors_rsi_15m`)

## Summary

The same rule as spec 0018.1 (buy a sharp dip inside an uptrend, exit once price is back above its 5 bar average), but on 15 minute bars with a 3 hour time stop. It needs no new code, only a study file, so it is the tracer that proves 15 minute bars end to end. The 1 hour version already judged the holdout year, so its result is labelled "holdout seen".

## Hypothesis

- **Why it might work:** forced selling overshoots on short timescales too. At 15 minutes there are about four times as many bars, so the 1 hour version's main problem (4 holdout trades) should go away.
- **When it should work:** steady trends with noisy pullbacks.
- **When it should fail:** when the snap back to the 5 bar average is smaller than about 1.5 × the 14 bps round trip. That is the likely fail: a 15 minute snap back is smaller than a 1 hour one, while the cost is the same.
- **No trade:** below the 200 bar average (about 50 hours) for longs, above it for shorts; during warmup (the first decision is possible at the close of bar 200, when SMA(200) is ready; RSI and ATR(14) are ready sooner).

## Rules

Exactly spec 0018.1 *Rules* and *Setup state*, unchanged code (`trading_core/strategy/connors_rsi.py`, `trading_core/node/connors_rsi.py`). Only the settings differ.

## Settings

| Field | Value | In grid |
|---|---|---|
| `bar_spec` | `"15-MINUTE-LAST"` | no |
| `atr_period` | 14 | no |
| `atr_floor_mult` | `"1"` | no |
| `trend_sma` | 200 | no |
| `exit_sma` | 5 | no |
| `stop_atr_mult` | `"3"` | no |
| `time_stop_bars` | 12 (3 hours) | no |
| `rsi_period` | | `[2, 3, 4]` |
| `rsi_lower` | | `[5, 10]` |

6 grid points. Study name `connors_rsi_15m`.

## Tests (AC-5)

No new rule code. The existing spec 0018.1 tests stand. Added:
- The `connors_rsi` config accepts `bar_spec = "15-MINUTE-LAST"` and its node subscribes to `PF_XBTUSD.KRAKEN-15-MINUTE-LAST-INTERNAL`.
- The look ahead prefix test runs `connors_rsi` at `15-MINUTE-LAST` (index AC-2).
- Verify step: after `lab study`, the stored strategy version config of each grid point carries `time_stop_bars` 12 and `bar_spec` `15-MINUTE-LAST` (the class default for `time_stop_bars` is 10, so a missing override would show here).

The exit at SMA(5) is often under 1 ATR from the entry, so this candidate does not meet the index's 1.5 ATR cost claim; task 1 records its typical exit distance in bps.

## Expected and risks

Many trades (likely well over 500 a year). The checks to watch are `fee_hurdle` and `holdout_sharpe`. The "holdout seen" label (AC-12) applies to any verdict.

## Short rationale

It costs one study file and tells you, before any new rule is written, whether the candle lab handles 15 minute bars at a reasonable speed and whether a well known pullback rule clears the cost at this timescale.

## Results

**Verdict: `fail`.** holdout seen: the same idea's 1 hour study (spec 0018.1) already judged this holdout year.

| Field | Value |
|---|---|
| Study | `connors_rsi_15m` (study file committed 2026-10-24 11:41 UTC in 7b3583c, study created 11:42 UTC) |
| Promotion id | `01a16ec6-ab2b-7395-84bc-e6f6994cf05c` (VPS) |
| Evaluation date | 2026-10-24 |
| Chosen grid point | 2 of 0 to 5 (`rsi_period` 3, `rsi_lower` 5), chosen in all 5 folds |
| Walk forward Sharpe | −1.084547 (5 folds, every fold negative; return −3.04%) |
| Holdout Sharpe | −0.825348, against buy and hold −0.460362 and the MACD baseline −1.377988 |
| Holdout return after costs | −0.56% |
| Holdout marked max drawdown | 0.73% |
| Deflated Sharpe probability | 0.000000 |
| PBO | 0.000000 |
| Holdout trades | 20 (12 long, 8 short; 18 exits at `target`, 2 stopped out; win rate 45%) |
| Trial count N, trial pool | 12, 12 (`kind+lab`, VPS) |
| `avg_hold_minutes` | 45.9 (longest 140) |
| Holdout gross per trade | 3.644 USD |
| Holdout cost per trade | 6.443 USD |

Reasons (code, value, threshold):
- `trade_floor`: `too_few_trades`, 20, 60
- `fee_hurdle`: `failed`, 3.644, 9.664
- `holdout_sharpe`: `failed`, −0.825348, 0
- `beats_buy_and_hold`: `failed`, −0.825348, −0.460362
- `walk_forward_sharpe`: `failed`, −1.084547, 0
- `bootstrap_lower_bound`: `failed`, −0.000049, 0
- `deflated_sharpe`: `failed`, 0.000000, 0.95

Reading: the trade rate was far below the expected 500 a year (about 20 a year in the holdout and 5 to 10 per half year fold), because RSI extremes beyond 5 above the 200 bar average are rare on 15 minute bars. When it did trade, the exit at SMA(5) gave a gross per trade about half the cost, as task 1's 11 bps median exit distance predicted. No revision (AC-13). The `time_stop_bars` 12 and `bar_spec` `15-MINUTE-LAST` overrides are in every grid point's stored strategy version config.
