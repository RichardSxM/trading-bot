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

Not run yet.
