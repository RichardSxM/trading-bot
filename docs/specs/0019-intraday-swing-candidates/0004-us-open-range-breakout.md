# 0019.4 US open range breakout (`us_open_breakout`)

## Summary

On each US weekday, mark the high and low of the first 30 or 60 minutes after the 09:30 New York stock market open. If a 15 minute bar then closes beyond that range within the next 2 hours, trade in the breakout's direction, with the stop at the range middle and a target of 1.5 or 2 times the risk. At most one trade a day, closed by 16:00 New York at the latest.

## Hypothesis

- **Why it might work:** the US cash open brings a burst of volume and news into BTC (ETF flows, equity correlation). A move that breaks the opening range often continues for hours, and its size at this hour is well above the 14 bps cost.
- **When it should work:** high volume weekdays with a clear macro direction.
- **When it should fail:** quiet days, when the break is a fake out and price comes back inside the range; weeks when BTC decouples from US equities.
- **No trade:** weekends; days where the range high equals its low; once the day's one signal is used.

## Rules

The bar's open time is `ts_ns − bar_interval_ns` (`BarInput` carries the close time only; the interval comes from `config.bar_interval_ns`, passed to the rule the same way `connors_rsi`'s node passes its settings). Per UTC date `d` of the bar's open time: `open_ns = ny_open_utc_ns(d)`, `range_end_ns = open_ns + range_bars × bar_interval_ns`, `window_end_ns = range_end_ns + entry_window_bars × bar_interval_ns`, `session_end_ns = open_ns + 6.5 h` (16:00 New York). Only days where `is_us_weekday(d)`.

- **Range:** the bars whose open time is at or after `open_ns` and whose close is at or before `range_end_ns`. `range_high` = their highest high, `range_low` = their lowest low. The range is ready at the close of the bar closing at `range_end_ns`. (15 minute bars align to 13:30 and 14:30 UTC, so the range is whole bars.)
- **Long signal:** range ready, the day's signal not yet used, the bar's close time after `range_end_ns` and at or before `window_end_ns`, `range_high > range_low`, and `close > range_high`. Reason `orb_long`. **Short signal:** the same with `close < range_low`, reason `orb_short`.
- **Structural stop:** the range middle, `(range_high + range_low) ÷ 2`. The base's floor then applies.
- **Exit, in this order:** reason `session_end` at the first bar close at or after `session_end_ns`, computed from the current bar's UTC open date (entries happen by 16:30 UTC at the latest, so the current bar's day is the entry's day until well after `session_end_ns`); else long when `close ≥ r_target(trade, target_r)`, short when `close ≤ r_target(trade, target_r)`, reason `target`.
- **Time stop:** `time_stop_bars` = 32 (8 hours), a backstop; `session_end` normally comes first.
- **`values`:** `day` (ISO date), `ny_open_ns`, `range_high`, `range_low` (or `None`), `range_ready`, `signal_used`.

## Setup state

Per day: `range_high`, `range_low`, `range_ready`, `signal_used`, and the `day` they belong to. They reset on the first bar whose open time's UTC date differs from `day` (keyed on the date, so a missing bar does not shift the reset). `consumed()` sets `signal_used` (so a refused entry is not retried that day). `on_flat()` does nothing. If the base refuses the entry (for example `BAD_SIGNAL`, when the quote mid has already gapped past the range middle stop), `consumed()` has still run, so that day's one signal is spent; this fails closed and is intended. A breakout while in a position cannot happen in practice (one trade a day) and would be dropped under the spec 0018 shared rule.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"15-MINUTE-LAST"` | no | must be `15-MINUTE-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `entry_window_bars` | 8 (2 hours) | no | ≥ 1 |
| `time_stop_bars` | 32 | no | base |
| `range_bars` | | `[2, 4]` (30 or 60 minutes) | ≥ 1 and ≤ 8 |
| `target_r` | | `["1.5", "2"]` | > 0 |

4 grid points. Config defaults for the grid fields are the grid's first point.

## Tests (AC-8)

- A summer weekday fixture (2024-07-10): the range is 13:30 to 14:30 UTC with `range_bars` 4, and a close above its high at the 14:45 bar gives one long, stop at the range middle.
- A winter weekday fixture (2024-01-10): the same at 14:30 to 15:30 UTC.
- A second close beyond the range later that day gives no second signal.
- A close above the range after `window_end_ns` gives no signal.
- A Saturday with the same price path gives no signal.
- An open long still open at 16:00 New York exits with `session_end`; one reaching 2 R first exits with `target`.
- A flat range (high equals low) gives no signal.
- A breakout whose entry is refused by the base gives no second signal that day.
- `range_bars = 0` and `bar_spec = "1-HOUR-LAST"` are refused by the config.

## Expected and risks

At most about 260 trades a year, likely 150 to 220 (not every day breaks inside the window), so the holdout should clear the 60 trade floor comfortably. US holidays are traded as normal days. The range middle stop is often closer than 1 ATR, so the floor will widen it.

## Short rationale

The opening range breakout is one of the oldest intraday rules and is fully rule based, with the session clock as its only new input. BTC's volume and volatility jump at the US open, so if any session effect clears the cost, this one should.

## Results

Not run. The AC-10 checkpoint on 2026-10-26 stopped after candidate 2: both earlier candidates earned less than the cost per trade before costs, and the trader chose to go back to `/architect` (see [verify.md](verify.md)). No code was written for this candidate.
