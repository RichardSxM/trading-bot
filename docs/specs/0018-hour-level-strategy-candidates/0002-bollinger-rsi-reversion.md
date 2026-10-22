# 0018.2 Bollinger and RSI reversion (`bollinger_rsi_1h`)

## Summary

When the market is moving sideways, buy a close back inside the lower Bollinger band after RSI showed an oversold extreme, and sell the mirror at the upper band. Take profit at the middle band. Trade only while the bands are flat, which is this candidate's own range test. It is named `bollinger_rsi_1h` because spec 0017 (scalping) registers its own 1 minute `bollinger_rsi`. The two are different strategies with separate modules, hashes, and trial families.

## Hypothesis

- **Why it might work:** in a range, a push outside the band that closes back inside is a failed push, and price tends to return to the range middle.
- **When it should work:** quiet sideways stretches (parts of 2023, summer 2024).
- **When it should fail:** at the start of a breakout from the range (the flat test lags by a few bars), and in trends, which the flat test should mostly keep it out of.
- **No trade:** when the bands are not flat; during warmup.

## Rules

Indicators on bar closes: `Bollinger(bb_period, bb_std)`, `Rsi(rsi_period)`, plus the base's ATR from `update`. Flat test at bar t: `|middle(t) − middle(t − flat_lookback)| ≤ flat_atr_mult × ATR(t)`. Ready when all have a value, the previous bar's bands and RSI are stored, and `flat_lookback` earlier middles are stored.

- **Long signal:** previous close < previous lower band, this close ≥ this lower band, previous bar's RSI < `rsi_lower`, and flat. Reason `bollinger_long`.
- **Short signal:** previous close > previous upper band, this close ≤ this upper band, previous bar's RSI > 100 − `rsi_lower`, and flat. Reason `bollinger_short`.
- **Structural stop:** the lowest low of the last 3 bars, this one included (long); the highest high (short). The base's ATR floor applies.
- **Exit:** long when `close ≥ middle`, short when `close ≤ middle`. Reason `target`.
- **Time stop:** `time_stop_bars` = 12.
- **`values`:** `middle`, `upper`, `lower`, `rsi_prev`, `flat_slope`.

## Setup state

None. The signal is a condition on this bar and the previous one; `consumed()` and `on_flat()` do nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bb_period` | 20 | no | ≥ 2 |
| `rsi_period` | 14 | no | ≥ 2 |
| `flat_lookback` | 5 | no | ≥ 1 |
| `flat_atr_mult` | `"0.5"` | no | > 0 |
| `time_stop_bars` | 12 | no | base |
| `bb_std` | | `["2", "2.5"]` | > 0 |
| `rsi_lower` | | `[25, 30]` | > 0 and < 50 |

4 grid points.

## Tests (AC-10)

- A flat fixture with a close below the lower band (RSI 22), then a close back inside, gives a long; a close at the middle band exits with `target`.
- The same pattern while the middle band moved 2 ATR in 5 bars gives no entry.
- The mirror fixture gives a short.
- The stop is the lowest low of the last 3 bars, or 1 ATR from the mid when that is closer.
- The Bollinger bands match a hand worked 20 close window (population standard deviation).

## Expected and risks

Both conditions plus the flat test make signals scarce, perhaps 40 to 120 a year, so `trade_floor` (60 holdout trades) may come back `insufficient`. If so, the revision options are a looser flat test or `rsi_lower` (index AC-18), not 15 minute bars (not part of this feature).

## Short rationale

This is the scope's Bollinger and RSI idea with the flat band rule made exact. The flat test is measured in ATRs so it means the same in calm and wild years. The stop sits past the push outside the band, which is where the "failed push" idea is proven wrong.

## Results

*No result yet.*
