# 0018.3 NR4 or inside bar breakout (`nr4_breakout`)

## Summary

After a quiet bar (one narrower than the three before it, or one inside the previous bar), enter in the direction of the first close beyond that bar's high or low. The quiet bar's other edge is the stop, and the target is a multiple of the risk.

## Hypothesis

- **Why it might work:** volatility clusters. A narrow bar is a pause, and the move that ends the pause tends to run.
- **When it should work:** before session opens and after news lulls, when the next move is directional.
- **When it should fail:** in choppy hours where breaks fail both ways. The close confirmation removes the shallowest false breaks, at the cost of a later entry.
- **No trade:** after `expiry_bars` without a close beyond the setup bar; during warmup.

## Rules

Bars plus the base's ATR. Ready after 4 bars and the ATR.

- **Setup bar S:** for `setup = "nr4"`, a bar whose range (high − low) is strictly below each of the 3 previous bars' ranges; for `setup = "inside"`, a bar with `high ≤ previous high` and `low ≥ previous low` (non strict, the standard definition, so a bar identical to the previous one counts). A setup is pending for the `expiry_bars` bars after S.
- **Order inside one `update`:** first test the pending setup's trigger with this bar's close, then test whether this bar is itself a new setup (it replaces any pending one, so a bar can trigger the old setup and become the next one).
- **Long signal:** while pending, the first bar that closes above `S.high`. Reason `nr4_long`. **Short signal:** the first that closes below `S.low`. Reason `nr4_short`.
- **Structural stop:** `S.low` (long), `S.high` (short). The base's ATR floor applies. No cap on the stop's distance: sizing from the stop already keeps the risk at 0.5%, and the time stop bounds a far R target.
- **Exit:** long when `close ≥ r_target(trade, target_r)`, short when `close ≤` it. Reason `target`.
- **Time stop:** `time_stop_bars` = 12.
- **`values`:** `setup_high`, `setup_low`, `setup_ts_ns`, `setup_age`.

## Setup state

- `pending` (S, its age) → cleared by `consumed()` after a signal, or on expiry, or replaced by a newer setup bar.
- In a position, setups keep being found and expiring; a trigger while in a position is consumed and dropped.
- `on_flat()` does nothing (a setup found during the trade stays pending).

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `expiry_bars` | 3 | no | ≥ 1 |
| `time_stop_bars` | 12 | no | base |
| `setup` | | `["inside", "nr4"]` | one of those |
| `target_r` | | `["1.5", "2.5"]` | > 0 |

4 grid points.

## Tests (AC-11)

- An NR4 fixture followed by a close above its high gives a long with the stop at its low (or the ATR floor), and a close past 1.5 R exits with `target`.
- A setup with no close beyond either edge in 3 bars expires with no entry.
- A newer inside bar replaces the pending setup.
- A bar that closes above the pending setup's high and is itself an inside bar triggers the old setup and becomes the new pending one.
- A trigger while in a position is dropped, and does not fire after the position closes.
- The mirror gives a short.
- A bar whose range equals one of the previous three is not NR4.

## Expected and risks

Setup bars are frequent on 1h, so trades are plentiful. Setup bars are narrow, so the ATR floor will often set the stop, and the R target then scales with ATR.

## Short rationale

From Kirkpatrick's volatility contraction idea, the scope's best early breakout candidate: few settings and fully rule based. Entering on a close beyond the level gives his close confirmation filter for free, so "with filter against none" is not a separate study here.

## Results

*No result yet.*
