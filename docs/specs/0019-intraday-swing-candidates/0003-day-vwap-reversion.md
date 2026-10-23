# 0019.3 Day VWAP reversion (`vwap_reversion`)

## Summary

The day's VWAP (volume weighted average price since UTC midnight) is a level many intraday traders watch. When price closes at least 2 to 3 ATR away from it and the bar turns back toward it, trade the snap back and exit when price reaches the VWAP, at the end of the UTC day, or after 2 to 4 hours.

## Hypothesis

- **Why it might work:** a large intraday stretch from VWAP is often a liquidity driven overshoot; once the forced flow is done, price tends to drift back toward the day's average, and a 2 ATR or larger distance to the target is several times the 14 bps cost.
- **When it should work:** range days and the hours after a liquidation cascade.
- **When it should fail:** trend days, where price walks away from VWAP and keeps going (the stop takes the hit); news days.
- **No trade:** the first `min_day_bars` bars of each UTC day (the VWAP is still unstable); the last `no_entry_last_bars` bars of each UTC day (a trade there would be cut short by `day_end` and pay the full cost for minutes of exposure); while VWAP or ATR is `None` (the first decision is possible at the close of bar 15, when ATR(14) is ready, and only once `min_day_bars` is met).

## Rules

`DayVwap` (index *Intraday helpers*) fed every bar with the bar's open time (`ts_ns − bar_interval_ns`, the interval from `config.bar_interval_ns`), high, low, close, and `BarInput.volume`, plus the base's ATR. Ready when ATR is ready (the VWAP's `None` only skips signals).

- **Late cutoff:** `entry_cutoff_ns` = the next UTC midnight after the bar's open time − `no_entry_last_bars × bar_interval_ns` (22:00 UTC with the default 8). It uses the clock, not `bars_in_day`, so a missing bar does not move it.
- **Long signal:** `bars_in_day ≥ min_day_bars`, the bar's open time before `entry_cutoff_ns`, VWAP not `None`, `close ≤ VWAP − stretch_atr × ATR`, and `close > open` (the bar turns up). Reason `vwap_long`.
- **Short signal:** the mirror: `close ≥ VWAP + stretch_atr × ATR` and `close < open`. Reason `vwap_short`.
- **Structural stop:** long `close − stop_atr_mult × ATR`, short `close + stop_atr_mult × ATR`. The base's floor then applies.
- **Exit, in this order:** reason `day_end` at the first bar of a new UTC day (its `bars_in_day` is 1), because the target level resets; else long when `close ≥ VWAP`, short when `close ≤ VWAP`, reason `target`.
- **Time stop:** `time_stop_bars` from the grid.
- **`values`:** `vwap`, `bars_in_day`, `stretch` (`(close − VWAP) ÷ ATR`, or `None`), `day` (ISO date).

## Setup state

None. The signal is a condition on the current bar only; `consumed()` and `on_flat()` do nothing.

## Settings

| Field | Value | In grid | Check |
|---|---|---|---|
| `bar_spec` | `"15-MINUTE-LAST"` | no | must be `15-MINUTE-LAST` |
| `atr_period` | 14 | no | base |
| `atr_floor_mult` | `"1"` | no | base |
| `min_day_bars` | 8 (2 hours) | no | ≥ 1 and < 96 |
| `no_entry_last_bars` | 8 (2 hours) | no | ≥ 0 and `min_day_bars + no_entry_last_bars` < 96 |
| `stop_atr_mult` | `"2"` | no | > 0 |
| `stretch_atr` | | `["2", "2.5", "3"]` | > 0 |
| `time_stop_bars` | | `[8, 16]` | base |

6 grid points. Config defaults for the grid fields are the grid's first point.

## Tests (AC-7)

- A fixture that drops to 2.2 ATR below the day VWAP on a down bar gives no signal; the next bar, still 2.1 ATR below and closing up, gives a long with the stop 2 ATR below its close.
- The mirror gives a short.
- The same stretch inside the first 8 bars of the day gives no signal.
- The same stretch on the 22:00 to 22:15 UTC bar gives no signal; on the 21:45 to 22:00 bar it gives one.
- An open long exits with `target` at the first close at or above the VWAP.
- An open long held over UTC midnight exits with `day_end` at the 00:00 to 00:15 bar's close.
- An open long after 8 bars (grid point) exits with `time_stop`.
- A day whose bars all have volume 0 gives no signal.
- No decision before the close of bar 15.
- `min_day_bars = 0`, `min_day_bars = 50` with `no_entry_last_bars = 50`, and `bar_spec = "1-HOUR-LAST"` are refused by the config.

## Expected and risks

Likely 150 to 400 trades a year at `stretch_atr` 2, fewer at 3. Mean reversion against a trend day is the main risk; the 2 ATR stop is the cap. At `stretch_atr` 2 the target is about 2 ATR away and the stop 2 ATR, so the best case is about 1 R and the rule needs to win more than about half its trades after costs; at 3 it is about 1.5 R. The late cutoff keeps entries out of the last 2 hours, but a trade entered at 21:45 can still be cut short by `day_end`.

## Short rationale

VWAP is the most used intraday anchor, it needs one new pure helper, and the candle lab has the volume it needs. UTC midnight keeps the day definition the same as spec 0018's `failed_breakout` and deterministic.

## Results

Not run yet.
