# 0018.6 Trend, RSI pullback, and MACD trigger, layer 2 (`trend_rsi_macd`)

## Summary

Layer 2 of your layered plan: layer 1 exactly, with its settings fixed at the values its holdout chose, plus a MACD timing trigger. After RSI crosses back out of the pullback zone, the entry waits up to 3 bars for the MACD histogram to turn in the trend's direction. The layer is kept only if it passes and beats layer 1 on unseen data.

## Hypothesis

- **Why it might work:** waiting for momentum to turn should skip pullbacks that keep falling after the RSI cross.
- **When it should work:** the same trends as layer 1, with fewer early entries.
- **When it should fail:** RSI and MACD are both built from recent price momentum, so the trigger may mostly agree with the RSI cross, adding settings without information. It may also enter later and give back the better price.
- **No trade:** as layer 1, plus when the histogram does not turn within the window.

## Rules

Everything in 0018.5 *Rules*, always on `1-HOUR-LAST` bars, with `trend_count` and `target_r` set from the original `trend_rsi_pullback` study's holdout grid point (index AC-20; never a revision's), plus `Macd(fast, slow, signal)` from `trading_core.strategy.indicators` on closes (ready when its histogram has two values).

- **Window:** layer 1's signal no longer enters; instead it opens a trigger window of `confirm_bars` bars, the RSI cross bar included. While the window is open, `pullback_low` (or `high`) keeps updating, and layer 1's arming is paused (a new 30 to 40 dip inside the window does not arm).
- **Long signal:** the first bar in the window with tag `up` and `histogram > previous histogram`. Reason `pullback_macd_long`. **Short signal** mirrors it with a falling histogram. Reason `pullback_macd_short`.
- **Window close:** at a signal (`consumed()`), after `confirm_bars` bars, or when layer 1 would disarm (RSI back through 30 or 70, which also latches; the tag changes). Then layer 1's arming resumes.
- **Stop, exit, time stop:** as layer 1.
- **`values`:** layer 1's, plus `histogram`, `histogram_prev`, `window_bar`.

## Setup state

Layer 1's states, plus `window` (open with its age, after the RSI cross) between `armed` and `idle`. In a position, the window keeps ageing and a signal in it is consumed and dropped. `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| layer 1's fixed fields | as 0018.5 | no | as 0018.5 |
| `trend_count`, `target_r` | layer 1's holdout grid point | no | as 0018.5 |
| `confirm_bars` | 3 | no | ≥ 1 |
| `signal` | 9 | no | ≥ 2 |
| `fast` | | `[8, 12]` | ≥ 2 and < `slow` |
| `slow` | | `[21, 26]` | > `fast` |

4 grid points.

## Tests (AC-14)

- Layer 1's long fixture with a histogram turning up at the cross bar gives the same entry as layer 1.
- With the histogram turning up 2 bars after the cross, the entry is 2 bars later and the stop includes the lower low in between.
- With no turn within 3 bars, no entry, and layer 1 can arm again afterwards.
- The RSI dropping through 30 inside the window closes it and latches.
- A second dip into 30 to 40 inside the window does not arm.
- The mirror gives a short.
- `fast = 26, slow = 21` is refused by the config.

## Expected and risks

Fewer trades than layer 1, so the trade floor gets closer. A layer that only removes trades can raise Sharpe by luck; the family N and the walk forward comparison of index AC-20 are there for that.

## Short rationale

This is the trigger layer of your plan, added on its own so its value is measured. The histogram turn is used rather than a signal line cross because it reacts faster, and `macd_baseline` (the benchmark) already tests the zero cross.

## Results

*No result yet.*
