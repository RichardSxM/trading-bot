# 0020.2 Noise area breakout with a time of day filter (`noise_breakout_tod`)

## Summary

Exactly candidate 1, with one change: no new entries on Sunday between 12:00 and 22:00 UTC, the window where Concretum found intraday trend trading lost money. Exits are not affected. It runs only if the checkpoint after candidate 1 says so (index AC-9).

## Hypothesis

- **Why it might work:** Concretum's 2018 to 2025 study found BTC intraday trend returns negative on US Sunday daytime (thin, directionless markets) and strongest from the Sunday evening Asia open into Monday. Skipping the weak window removes losing whipsaws while keeping the strong window.
- **When it should work:** when the weekly pattern still holds after the US ETF launches.
- **When it should fail:** if the pattern was an artefact of the study's sample, or it has faded; then this is candidate 1 with about 6% fewer entries and nothing gained.
- **No trade:** candidate 1's cases, plus any bar whose open time is a Sunday with 12:00 ≤ time of day < 22:00 UTC.

## Rules

`trading_core/strategy/noise_breakout_tod.py` builds on the frozen `noise_breakout` rule. It imports it and does not copy or change it:

- `NoiseBreakoutTodRule` subclasses `NoiseBreakoutRule` and overrides only `entry` and `values`. Its node config subclasses `NoiseBreakoutConfig`, so the base `__post_init__` checks (including `bar_spec`) still run, and it sets its own strategy name `noise_breakout_tod`. Its code closure then holds `strategy/noise_breakout.py`, `noise_area.py`, and candidate 1's node module, which is fine, since those are frozen.
- `update`, `ready`, `exit`, `consumed`, `on_flat`: exactly candidate 1's (inherited).
- `entry()`: `None` when the current bar's open time (`ts_ns − bar_interval_ns`) is a UTC Sunday and its time of day is at or after 12:00 and before 22:00; otherwise candidate 1's `entry()`.
- **`values`:** candidate 1's, plus `tod_blocked` (bool).

## Setup state

Candidate 1's. The filter is stateless: it reads only the current bar's open time.

## Settings

Candidate 1's settings and checks, with the same 3 point grid (`band_mult` `["1", "1.5", "2"]`), so the filter is compared like for like. The window bounds are fixed in the module (not settings, not in the grid).

## Tests (AC-6)

- A Sunday 14:00 UTC breakout that gives a candidate 1 long gives no signal here.
- The same breakout on Sunday 22:00 UTC, and on a Saturday at 14:00, gives the long.
- A long opened Sunday 11:30 UTC still exits with `noise_trail` at 13:00 (exits are not filtered).
- Every candidate 1 test outside the window passes unchanged.
- `noise_breakout`'s `code_hash` is the same before and after this module lands (index AC-1).
- The registry gives the two strategies different names.

## Expected and risks

About 20 fewer entry bars per week out of 336, so a trade count close to candidate 1's. Carries the index AC-11 label: the filter comes from a published sample (2018 to 2025) that may overlap part of our holdout year. No revision (index AC-12).

## Short rationale

One fixed, published window, stateless and cheap, is the smallest honest test of whether time of day helps a trend rule; it adds 3 trials to N.

## Results

Not run. The AC-9 checkpoint on 2026-10-26 skipped it: `noise_breakout` earned 0.19 USD gross per trade against 6.28 USD of cost, and a time of day filter only removes trades, so it cannot lift a below cost signal past the hurdle. The trader chose to skip it and still run `donchian_4h` (see [verify.md](verify.md)). No code was written for this candidate, and it adds nothing to N.
