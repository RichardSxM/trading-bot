# Review, feat/0020-close-gates, 2026-10-27

**Reviewed by**: Sonnet 5.5 (author on another model)
**Scope**: 20 files, branch vs base 02a0cc5 (spec 0020 files only, spec 0021 left out)
**Verdict**: Approve with nits

## Summary
The change adds three research candidates on the shared bar rule base: noise_breakout (30 minute bars), donchian_4h, and donchian_4h_r1 (the same rule on 1 hour bars, as its own module). The pure rules match the child specs line by line, including the entry day memory, the frozen line after midnight, and the channel read before the bar joins the window. I found no blockers and no majors. The findings are small edge cases and nits. These modules are frozen after their first study, so any fix means a new module and a new study, not an edit.

## Minor
### 🟡 The Donchian rule trusts its caller for entry_bars, `packages/core/src/trading_core/strategy/donchian.py:42`
**Problem**: `DonchianRule` sets `exit_bars = entry_bars // 2`. With `entry_bars = 1` the exit window has size 0, and `_channel` would then call `max` on an empty deque and raise. The config refuses `entry_bars < 2`, so this cannot happen through the node, but the pure rule has no check of its own.
**Why it matters**: Only a risk if someone builds the rule directly with a new caller. Today the base catches the error and fails closed, so no wrong order follows.
**Suggested fix**: Nothing now. The module is frozen. If a later candidate reuses the rule, validate `entry_bars >= 2` in that new module's constructor.

### 🟡 Entry day lookup assumes one `update` per bar, `packages/core/src/trading_core/strategy/noise_breakout.py:96`
**Problem**: `_trade_line` finds the entry day with `days[-1 - bars_held]`. This is right when `bars_held` counts the same bars that were fed to `update`. The base increments `bars_held` only while in a position and only after `ready()`, and `update` runs on every bar. While in a position both count the same bars, so it holds today. It also relies on the bar before the entry being the decision bar, which the test with a missing bar covers.
**Why it matters**: The coupling is implicit. A future change to when the base counts bars would silently pick the wrong day's frozen line.
**Suggested fix**: Keep it as is, since the module is frozen. The existing tests pin the behaviour. Note the coupling in the base's docs if the base is touched.

### 🟡 Cost check script keeps partly filled windows, `scripts/spikes/cost_check_trend.py:47`
**Problem**: The docstring says a window with no stored candle is skipped, but a window with only some of its minute candles still becomes a bar. Live aggregation may differ (Nautilus gives a zero volume bar for empty windows, per the lab test).
**Why it matters**: The numbers feed a go or no go note in the spec, so small differences in the median ATR or band share could mislead. It is a throwaway script and not a trial.
**Suggested fix**: Say in the docstring that partial windows are kept, or drop windows with fewer than the full minute count.

### 🟡 The round trip cost is hard coded in the spike, `scripts/spikes/cost_check_trend.py:41`
**Problem**: `ROUND_TRIP_BPS = 14` is written out, not read from the study settings (taker 5 bps, slippage 2 bps).
**Why it matters**: If the study costs change, this script quietly disagrees.
**Suggested fix**: Add a comment with the source, or read it from a study file.

## Nits
- ⚪ `packages/core/src/trading_core/strategy/noise_area.py:60`, parameter `open` shadows the builtin; `open_` or `open_px` reads safer.
- ⚪ `packages/core/src/trading_core/strategy/noise_area.py:8`, the freeze note cites spec 0018 AC-7, but this module is governed by spec 0020 AC-1 and AC-12; fix it in a new module only.
- ⚪ `packages/core/src/trading_core/strategy/noise_breakout.py:75`, the day_lines prune builds `set(self.days)` on every bar; harmless, but one set per pass is enough.
- ⚪ `apps/lab/tests/test_lookahead.py:38`, the CONFIGS value type includes `bool`, which no entry uses.
- ⚪ `packages/core/src/trading_core/node/donchian_4h_r1.py:1`, near copy of `donchian_4h.py` by design (own code hash); the docstring says so, good.
- ⚪ `scripts/spikes/cost_check_trend.py:50`, the helper functions have no docstrings; not required outside core.

## Strengths
- The rules follow the specs closely: bands from earlier days only, the VWAP and bands keyed by bar open time so the 23:30 bar stays on the old day, and the Donchian channel read before the bar is added.
- Fail closed choices are right: a missing 00:00 bar gives no bands that day, a None line means no trail exit and the time stop still closes the trade.
- The Donchian R1 revision is its own module, its own study file, and its own registry entry, so the frozen donchian_4h hash is untouched. The `hash_check.py` script makes that checkable.
- All math is Decimal with no rounding inside the rules. Every public class and function in core has a docstring.
- The lookahead test is extended to the new candidates, and the registry test pins the hour level list.

## Test coverage
Good. Pure rule tests cover each spec bullet: signal and mirror, equal close gives no signal, the current bar not in its own channel, exit channel of half the entry length, crash bar then reversal, zero volume bars, readiness that never turns false, midnight freeze, a missing bar between entry and exit, and a None line the day before. Noise helper tests cover slot math, missing slots and days, and the history limit. Node tests check defaults, bad settings, and the smallest allowed settings. Lab tests run each candidate through the candle lab, check that wrong bar specs are refused before any run, and check each study file keeps the shared settings and the grid size. I see no untested branching logic.
