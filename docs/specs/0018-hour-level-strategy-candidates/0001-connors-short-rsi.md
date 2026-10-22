# 0018.1 Connors short RSI (`connors_rsi`)

## Summary

Buy short, sharp dips inside an uptrend and sell short, sharp rallies inside a downtrend, then get out as soon as price is back above (or below) its 5 bar average. RSI with a very short period (2 to 4) finds the dips, and the 200 bar average sets the trend. It is the first candidate, so it also proves the shared base.

## Hypothesis

- **Why it might work:** on BTC 1h, a sharp drop inside an uptrend is often forced selling (stops, liquidations) that overshoots and partly reverts within hours once that flow ends.
- **When it should work:** steady trends with noisy pullbacks (much of 2023 and 2024).
- **When it should fail:** when a trend breaks (May and November 2022 crashes), on news, and whenever the average snap back is smaller than about 1.5 × the round trip cost (taker fee twice plus 2 bps slippage each side, about 0.14% of notional). The fee hurdle is the most likely fail.
- **No trade:** below the 200 bar average for longs, above it for shorts; during warmup (200 bars).

## Rules

Indicators on bar closes: `Rsi(rsi_period)`, `Sma(trend_sma)`, `Sma(exit_sma)`, plus the base's ATR from `update`. Ready when all have a value.

- **Long signal:** `close > SMA(trend_sma)` and `RSI < rsi_lower`. Reason `connors_long`.
- **Short signal:** `close < SMA(trend_sma)` and `RSI > 100 − rsi_lower`. Reason `connors_short`.
- **Structural stop:** `close − stop_atr_mult × ATR` (long), `close + stop_atr_mult × ATR` (short). The base's floor then applies.
- **Exit:** long when `close > SMA(exit_sma)`, short when `close < SMA(exit_sma)`. Reason `target`. When the entry close was already beyond SMA(exit_sma), this exits at the next bar close (`bars_held` 1); that is intended.
- **Time stop:** `time_stop_bars` = 10.
- **`values`:** `rsi`, `sma_trend`, `sma_exit`.

## Setup state

None. The signal is a condition on the current bar only; `consumed()` and `on_flat()` do nothing. If the condition still holds at the next bar close after a refusal, that is a new signal.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `trend_sma` | 200 | no | ≥ 2 |
| `exit_sma` | 5 | no | ≥ 2 |
| `stop_atr_mult` | `"3"` | no | > 0 |
| `time_stop_bars` | 10 | no | base |
| `rsi_period` | | `[2, 3, 4]` | ≥ 2 |
| `rsi_lower` | | `[5, 10]` | > 0 and < 50 |

6 grid points.

## Tests (AC-9)

- A fixture where RSI(2) falls below 5 with the close above SMA(200) gives one long at that close with the stop 3 ATR below the close; the first close above SMA(5) exits with `target`.
- The mirror fixture gives a short.
- The same dip below SMA(200) gives no long.
- A long still open after 10 bar closes exits with `time_stop`.
- No decision before 200 bars.
- `rsi_lower = 50` is refused by the config.

## Expected and risks

Many signals (likely 150 to 400 a year, less the holdout's first 8 days of warmup), so the trade floor is safe. The average win is small and quick, so `fee_hurdle` and `holdout_sharpe` are the checks to watch.

## Short rationale

Connors' short RSI is one of the most tested pullback rules, has very few settings, and decides only on closes, which makes it the thinnest tracer through the new base. The 3 ATR stop is wide on purpose: mean reversion rules lose much of their edge with tight stops, and the time stop bounds the hold instead.

## Results

*No result yet.*
