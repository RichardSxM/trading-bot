# 0018.5 Trend and RSI pullback, layer 1 (`trend_rsi_pullback`)

## Summary

Layer 1 of your layered plan. Price action decides the trend: count how many of the last 15 closes sit above the 20 EMA. In an uptrend, wait for RSI(14) to dip into 30 to 40, then buy when it crosses back above 40. Mirror it in a downtrend. The stop goes under the pullback's low, the target is a multiple of the risk.

## Hypothesis

- **Why it might work:** in a trend, pullbacks are where late participants join; buying the end of a shallow pullback joins the trend at a better price than a breakout.
- **When it should work:** sustained trends (late 2023 to early 2024, late 2024).
- **When it should fail:** at trend ends (the last pullback becomes the reversal) and in ranges that the 15 close tag misreads as trends.
- **No trade:** when the tag reads range; when RSI drops through 30 (the pullback became a reversal) until RSI recovers above 40.

## Rules

Indicators on bar closes: `Ema(ema_period)` from `trading_core.strategy.indicators`, `Rsi(rsi_period)`, plus the base's ATR. At each bar, after updating, store whether `close > EMA` and whether `close < EMA`. Ready when `tag_window` such flags and the RSI exist.

- **Trend tag:** `up` when at least `trend_count` of the last `tag_window` closes are above their EMA; `down` when at least `trend_count` are below; otherwise `range`.
- **Arm long:** tag `up`, `30 ≤ RSI ≤ 40`, and not latched. `pullback_low` starts at the arm bar's low and takes the lowest low of every later bar while armed, the signal bar included.
- **Disarm long:** RSI < 30 (this also sets the long latch), or the tag is no longer `up`. The **latch** blocks arming again until RSI closes above 40; it is cleared by that close.
- **Long signal:** armed at the previous bar, this RSI > 40, tag `up`. Reason `pullback_long`.
- **Short side** mirrors it: tag `down`, arm at `60 ≤ RSI ≤ 70`, track `pullback_high`, disarm and latch at RSI > 70 (cleared by a close with RSI below 60), disarm when the tag leaves `down`, signal when RSI drops below 60. Reason `pullback_short`.
- **Structural stop:** `pullback_low` (long), `pullback_high` (short). The base's ATR floor applies.
- **Exit:** the R target, as in 0018.3. Reason `target`.
- **Time stop:** `time_stop_bars` = 48.
- **`values`:** `ema`, `above_count`, `below_count`, `tag`, `rsi`, `rsi_prev`, `pullback_extreme`, `latched`.

## Setup state

- Per side: `idle` → `armed` → `idle` on a signal (`consumed()`), a disarm, or the tag changing; `latched` after RSI breaks through 30 (or 70) until it recovers past 40 (or 60).
- In a position, arming and latching keep updating; a signal while in a position is consumed and dropped.
- `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `ema_period` | 20 | no | ≥ 2 |
| `tag_window` | 15 | no | ≥ 1 |
| `rsi_period` | 14 | no | ≥ 2 |
| `time_stop_bars` | 48 | no | base |
| `trend_count` | | `[10, 12]` | > `tag_window ÷ 2` and ≤ `tag_window` |
| `target_r` | | `["2", "3"]` | > 0 |

4 grid points. The RSI zones (30 to 40, 60 to 70) are fixed, from the scope. The `trend_count` check keeps `up` and `down` from both holding at once.

## Tests (AC-13)

- An uptrend fixture where RSI dips to 35 and crosses back to 42 gives a long with the stop at the pullback low (the arm bar's low included).
- A dip to 28, then RSI back to 35, then a cross to 42 gives no entry (latched until a close above 40, which here is the cross itself, so it clears the latch without a signal).
- A fixture with 9 of 15 closes above the EMA (`trend_count` 10) is `range` and gives no entry.
- A long hitting 2 R exits with `target`; one open after 48 bars exits with `time_stop`.
- The mirror gives a short.
- `trend_count = 7` with `tag_window = 15` is refused by the config.

## Expected and risks

Fewer trades than the reversion candidates (maybe 60 to 150 a year), longer holds, and long losing streaks in choppy years, which the 0.5% risk is sized for. BTC's long trend may weight profit to the long side and fail `concentration_direction`.

## Short rationale

This is the scope's "RSI pullback in a trend" idea with the trend coming from your price action context (the 20 EMA and 15 close tag) instead of a 50 bar average. The latch makes "dips into 30 to 40" mean a shallow pullback, not a crash that bounces. It is the base layer your plan builds on, so layer 2 has something measured to beat.

## Results

*No result yet.*
