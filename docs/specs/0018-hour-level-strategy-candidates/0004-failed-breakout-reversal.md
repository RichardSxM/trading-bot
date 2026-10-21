# 0018.4 Failed breakout reversal (`failed_breakout`)

## Summary

When price pokes above yesterday's high (or below yesterday's low) and closes back inside, wait for a close through the most recent confirmed swing in the other direction, then trade the reversal. The stop goes past the extreme of the failed poke. This one candidate covers both the liquidity sweep idea and Kirkpatrick's failed breakout reversal.

## Hypothesis

- **Why it might work:** the prior day's high and low hold resting stops and breakout orders. A poke that triggers them and then fails leaves late breakout traders trapped, and their exits fuel the reversal.
- **When it should work:** range days and the end of exhausted moves.
- **When it should fail:** on real trend days, where the level breaks, briefly retests, and continues; the swing break requirement is meant to filter those.
- **No trade:** before a full prior UTC day of bars exists; after the setup expires; when price closes back beyond the level (the breakout was accepted).

## Rules

Bars plus `SwingTracker(swing_bars, swing_bars)` (spec 0014 AC-20) and the base's ATR. The rule gets `bar_interval_ns` at construction; `bars_per_day = 86 400 000 000 000 ÷ bar_interval_ns` (24 for 1h, 6 for 4h; a `bar_spec` that doesn't divide a day evenly is refused by the config).

- **Levels:** a bar belongs to the UTC day of its open time (`ts_ns − bar_interval_ns`). At the first bar of a UTC day, before any other test on that bar, `PDH` and `PDL` become the highest high and lowest low of the bars that opened in the previous UTC day. They are set only when that day has all `bars_per_day` bars (hour bars always build, spec 0014 AC-11, so this fails only at the range start); otherwise there are no levels that day.
- **Swings:** every bar goes to `SwingTracker`; the rule keeps the confirmed swings (`confirmed_index` ≤ the current bar index, so never a peek).
- **Sweep (short side):** a bar with `high > PDH` and `close < PDH` arms a short setup, at most once per side per UTC day. It stores `sweep_extreme` = its high and `swing_level` = the low of the most recent swing low confirmed at or before the sweep bar (no such swing: it doesn't arm). While armed, `sweep_extreme` takes the max of later highs. **Sweep (long side)** mirrors it at `PDL` with lows and the most recent confirmed swing high.
- **Disarm:** a close above the swept `PDH` (short side; below `PDL` for the long side), or `confirm_bars` bars after the sweep bar without a signal. An armed setup stays armed across midnight (it keeps the level it swept).
- **Short signal:** while armed, on a bar after the sweep bar (the sweep bar itself never triggers), a close below `swing_level`. Reason `sweep_short`. **Long signal** mirrors it above the stored swing high. Reason `sweep_long`.
- **Both sides armed:** the first signal wins; `consumed()` disarms both sides.
- **Structural stop:** `sweep_extreme` (short), the sweep low (long). The base's ATR floor applies.
- **Exit:** the R target, as in 0018.3. Reason `target`.
- **Time stop:** `time_stop_bars` = 24.
- **`values`:** `pdh`, `pdl`, `sweep_extreme`, `swing_level`, `armed_side`, `bars_since_sweep`.

## Setup state

- Per side: `idle` → `armed` (sweep) → `idle` on a signal (`consumed()`), disarm, or expiry. The once per side per day counter resets at each UTC day.
- In a position, sweeps still arm and expire; a signal while in a position is consumed and dropped.
- `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `confirm_bars` | 6 | no | ≥ 1 |
| `time_stop_bars` | 24 | no | base |
| `swing_bars` | | `[2, 3]` | ≥ 1 |
| `target_r` | | `["1.5", "2.5"]` | > 0 |
| (`bar_spec`) | base | | divides one UTC day |

4 grid points.

## Tests (AC-12)

- A fixture day, then a bar that pokes above its high and closes inside, then a later close below the stored swing low, gives a short with the stop at the poke's high (or the ATR floor).
- A close above `PDH` before the signal disarms it.
- A second poke of the same level the same UTC day does not arm again; a poke of the other level does.
- A sweep bar that also closes below the swing low does not trigger; the next bar can.
- A swing confirmed only after the sweep bar is not used.
- An armed setup survives midnight and triggers on the new day.
- The first day of a range sets no levels.
- On `4-HOUR-LAST` bars a day is 6 bars.
- The mirror gives a long.

## Expected and risks

Clean sweeps of the prior day's level happen a few times a week, and the swing break filters many, so holdout trades may land near the 60 floor. `concentration_top_trades` may also bite, since a few big reversal days could carry the profit.

## Short rationale

The scope lists the liquidity sweep twice (market structure guide, `strategy.md`) and Kirkpatrick's failed breakout; testing them as one candidate keeps N down. Prior day levels come from the strategy's own bars, so there is no extra subscription and live behaves the same. Swings use the existing confirmed only tracker, and the swing to break is fixed at the sweep, so the setup can't wait for a far away swing.

## Results

*No result yet.*
