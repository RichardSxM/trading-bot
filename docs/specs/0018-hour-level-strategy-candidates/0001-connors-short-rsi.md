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

- **Study:** `connors_rsi` (study id `01a15fdb-c081-732e-8d3c-c19ef136f8e2`), frozen pick grid point 2 (`rsi_period` 4, `rsi_lower` 5), holdout run `21f3ae66-8311-4b9f-8144-5879e98c5594`
- **Promotion:** `01a1631b-5be2-7006-b2ab-49047e5ff6b9`, evaluated 2026-10-22 on the VPS (release 5fbd615)
- **Verdict:** `fail`

Reasons:

| Check | Code | Value | Threshold |
|---|---|---|---|
| `trade_floor` | `too_few_trades` | 4 | 60 |
| `walk_forward_sharpe` | `failed` | −0.454449 | 0 |
| `bootstrap_lower_bound` | `failed` | −0.000001 | 0 |
| `deflated_sharpe` | `failed` | 0.000018 | 0.95 |
| `concentration_direction` | `failed` | 0.961015 | 0.60 |

Metrics:

| Metric | Value |
|---|---|
| Walk forward Sharpe (5 folds) | −0.454449 |
| Holdout Sharpe | 0.956926 |
| Buy and hold Sharpe | −0.460362 |
| MACD baseline Sharpe | −1.377988 |
| Holdout return after costs | 0.001964 (+0.20%) |
| Holdout max drawdown (marked) | 0.001479 |
| Deflated Sharpe probability | 0.000018 |
| PBO | 0.000000 |
| Holdout trades | 4 |
| Trial count N | 6 |
| Trial pool | 6 (`trial_scope` `kind+lab`) |

**N0 = 6.** No other `hour_level` candle study was on the VPS, so the family N at the first evaluation is this study's own 6 grid points.

The "many signals" expectation was wrong: with `rsi_lower` 5 and the 200 bar trend filter, the frozen pick traded only 4 times in 364 holdout days, far below the floor of 60. The whole grid lost money in the study, mostly to fees, and the holdout gain rests on 4 trades in nearly one direction.
