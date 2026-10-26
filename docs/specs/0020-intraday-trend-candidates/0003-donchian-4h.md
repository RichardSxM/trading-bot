# 0020.3 Donchian channel trend on 4 hour bars (`donchian_4h`)

## Summary

The classic "turtle" trend rule on 4 hour bars. Go long when a close breaks above the highest high of the previous 20 (or 55) bars, short when it breaks below the lowest low. Exit when a close breaks the opposite channel of half that length. The stop sits 2 or 3 ATR away, and no trade lasts more than 10 days. It holds for days, so each winner aims for a move far above the cost.

## Hypothesis

- **Why it might work:** "Catching Crypto Trends" (Zarattini, Pagani, Barbon, 2025) finds Donchian channel trend models on crypto give a Sharpe above 1.5 after fees (on a 20 coin portfolio; BTC alone is weaker). Multi day trends are the best documented crypto edge, and a move of several percent dwarfs 14 bps.
- **When it should work:** long trending stretches (2023 to 2024 style rallies, sharp sell offs).
- **When it should fail:** range bound months, when every break reverses; and if BTC's holdout year is flat or choppy.
- **No trade:** until `entry_bars` earlier bars and ATR(14) exist; while a position is open; on the bar the base exits.

## Rules

`trading_core/strategy/donchian.py`. Per bar, the channels are read **before** the current bar is added:

- `entry_high` / `entry_low`: the highest high and lowest low of the previous `entry_bars` bars. `exit_bars = entry_bars ÷ 2` rounded down (10 or 27); `exit_high` / `exit_low`: the same over the previous `exit_bars` bars.
- **Long signal:** `close > entry_high`. Reason `donchian_long`. Structural stop `close − stop_atr × atr`. **Short signal:** `close < entry_low`. Reason `donchian_short`. Structural stop `close + stop_atr × atr`. The base's 1 ATR floor is closer, so the structural stop is the one used (unless the mid moved).
- **Exit:** a long exits when `close < exit_low`, a short when `close > exit_high`, reason `channel_exit`.
- **Time stop:** `time_stop_bars` = 60 (10 days), in the base.
- After reading, the current bar's high and low are appended to both windows (deques with `maxlen` `entry_bars` and `exit_bars`).
- **`values`:** `entry_high`, `entry_low`, `exit_high`, `exit_low` (each `None` until its window is full).
- **Constructor:** `DonchianRule(*, entry_bars, stop_atr)`. `stop_atr` is a `Decimal`. All maths uses the default `Decimal` context with no rounding inside the rule; `floored_stop` rounds the stop to the tick.

## Setup state

The two windows and the last ATR. `ready()` is true once the entry window is full and an ATR has been seen, and never turns false. The signal is a level condition, so `consumed()` does nothing. In principle a close still beyond the channel after an exit may enter again, but in practice that is rare: the base never enters on the exit bar, and by the next bar the window includes the exit bar's own high or low. So a reversal (a long's `channel_exit` in a crash, then a short) comes at least one bar late, and often not at all. `on_flat()` does nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"4-HOUR-LAST"` | no | must be `4-HOUR-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `time_stop_bars` | 60 | no | base |
| `entry_bars` | | `[20, 55]` | ≥ 2 |
| `stop_atr` | | `["2", "3"]` | ≥ 1 |

4 grid points. Config defaults for the grid fields are the grid's first point.

## Tests (AC-7)

- A rising fixture: the first close above the previous 20 bar high gives a long, stop `close − 2 × ATR`.
- The mirror case gives a short.
- A close equal to the previous high gives no signal (strictly above).
- The current bar's own high is not in the channel it is compared against.
- A long exits with `channel_exit` at the first close below the previous 10 bar low.
- A long still open after 60 bars exits with `time_stop` (base).
- No signal before 20 earlier bars.
- A long that exits with `channel_exit` on a crash bar gets no short on that bar; the next bar gives a short only if its close is below the updated 20 bar low, which now includes the crash bar.
- `entry_bars = 1`, `stop_atr = "0.5"`, and `bar_spec = "1-HOUR-LAST"` are refused by the config.

## Expected and risks

Few trades: perhaps 30 to 80 a year at 20 bars and 15 to 30 at 55, so the holdout may fall under the 60 trade floor and give `insufficient`. That is an honest result, not a reason to lower the floor. Holds of days pay funding, which the lab charges. A valid revision under AC-12 is a switch to `1-HOUR-LAST` bars for more trades.

## Short rationale

The best documented crypto trend rule, with only two settings, and a timescale none of the earlier candidates tested. It is independent of candidates 1 and 2, so the checkpoint keeps it even if they fall below cost.

## Results

Not run yet.
