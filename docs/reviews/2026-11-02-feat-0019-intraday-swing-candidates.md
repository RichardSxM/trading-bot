# Review, feat/0019-close-gates, 2026-11-02

**Reviewed by**: Sonnet 5.5 (author on another model)
**Scope**: 8 code files plus 7 test files, already merged, commits 7b3583c, 8facad6, 24216d9, 5e72221 (candidates 3 and 4 were never built on purpose, so they are not judged)
**Verdict**: Approve with nits

## Summary
The change adds a bar interval aware `cut_times` for the look ahead check, a 15 minute study file for `connors_rsi`, the new `ema_pullback` candidate (pure rule in core `strategy/`, thin Nautilus node in `node/`, registry entry in the lab), its study file, and a throwaway cost check script. The pure rule follows the child spec step by step, including the disarm then re-arm order, the arm bar counting as 1, and the trigger on the arming bar. I found no blockers and no majors. The findings are small gaps in tests and in the spike script. Both studies are frozen and already failed the gate, so any fix means a new module and a new study, not an edit.

## Minor
### 🟡 The ema_pullback study test does not pin the fixed settings or the grid, `apps/lab/tests/test_intraday_swing_studies.py:27`
**Problem**: The shared test only checks the shared settings, the bar spec, and that the grid has 4 to 6 points. The `connors_rsi_15m` file also gets a test that pins its time stop and its 6 grid points. `ema_pullback.toml` gets no such test, so its `target_r` and `rsi_high` grid, `time_stop_bars = 32`, `fast`, `slow`, and `arm_bars` are not checked against the child spec.
**Why it matters**: The plan is frozen by name. A silent edit to the grid would still pass. The hash check and the commit time check in verify.md cover this by hand, but not in the test suite.
**Suggested fix**: Nothing to do to the frozen file. If a revision `ema_pullback_r1` is ever made, give its study file a test that pins the grid and the fixed settings.

### 🟡 The cost check script keeps partly filled windows and hides its inputs, `scripts/spikes/cost_check_15m.py:5`
**Problem**: The docstring says a 15 minute window with no stored candle is skipped. A window with only some of its 1 minute candles still becomes a bar (`close_window` only checks that the list is not empty). Also, the ATR and SMA keep running across a data gap, and `ROUND_TRIP_BPS = 14` is typed in by hand, not read from a study file.
**Why it matters**: The numbers (median ATR 29.21 bps, 3.13 times cost) fed the go decision recorded in verify.md. Small differences could shift the multiples. It is a throwaway script and not a trial, so the risk is low.
**Suggested fix**: Say in the docstring that partial windows are kept, or drop windows with fewer than 15 candles. Add a comment saying where the 14 comes from (taker 5 bps and slippage 2 bps, each side).

### 🟡 Look ahead cuts use only the first variant's bar spec, `apps/lab/src/lab/candle_backtest.py:645`
**Problem**: The cut interval comes from `variants[0].grid_point.get("bar_spec", loaded.bar_spec)`. If a grid ever varied `bar_spec`, the other variants would be cut at the first variant's interval, which may fall on one of their bar closes.
**Why it matters**: Today only the first variant is cut at all (AC-19, noted in the comment above it) and no grid varies `bar_spec`, so nothing is wrong. The assumption is implicit.
**Suggested fix**: Add one sentence to the comment that cuts assume one bar spec across a study. A later change could refuse a grid that varies `bar_spec`.

## Nits
- ⚪ `packages/core/src/trading_core/strategy/ema_pullback.py:88`, `_disarms` returns True when `slow is None`, but an armed rule always has a slow EMA (an arm needs an up or down trend). The branch is dead code; it only exists to satisfy the type of `slow`.
- ⚪ `apps/lab/studies/ema_pullback.toml:5`, the header says a change is "a revision", while `connors_rsi_15m.toml:8` says "give the study a new name". Same rule, two wordings.
- ⚪ `apps/lab/tests/test_lookahead.py:94`, the `"1-HOUR-LAST"` default for the bar spec repeats a default that lives in the config; fine in a test, but it will drift if the base default changes.
- ⚪ `packages/core/src/trading_core/node/ema_pullback.py:49`, the config checks do not mention the time stop. `time_stop_bars` is left to the base, which is right; a comment saying so would help the next reader.

## Strengths
- `ema_pullback` rule is pure Decimal. No floats, no Nautilus imports in `strategy/`, and every public class and method has a docstring. The node file only builds the config and the rule, as the architecture rule asks.
- The rule matches the spec edge cases: the arm bar counts as 1, the sixth bar can trigger and the seventh cannot, a stale arm can re-arm on the same bar with its own extreme, and a trigger in a position is dropped by the base calling `consumed()`.
- `cut_times` keeps the old hour draws exactly: the test pins the numbers from before the change, so earlier look ahead runs stay valid. It also refuses intervals that have no whole minute strictly between closes.
- Look ahead coverage is extended to `connors_rsi` at 15 minutes and to `ema_pullback`, and the test asserts the run made decisions so an empty run cannot pass quietly.
- Config validation follows the child spec (`fast` below `slow`, `rsi_high` at most 50, bar spec fixed), and a study on hour bars is refused before any run.
- No existing strategy module changed, so no existing code hash moved; verify.md records the hash check.

## Test coverage
Good. The pure rule has a test for each bullet of the child spec: long and short fixtures, the same bar trigger, RSI above the band, disarm on a close below the slow EMA, the sixth and seventh bar, the re-arm on a stale bar, a consumed trigger, a target exit for long and short, readiness at bar 21, and the recorded values. Node tests check defaults, the 15 minute subscription, and bad settings. Lab tests cover the registry entry, the startup check, the refusal of hour bars, and a run through the candle lab with floored stops. Core tests cover the new cut interval, including the unchanged hour draws and the refused intervals. The only gap is the missing pin of the ema_pullback grid and fixed settings (see the first minor finding). The time stop exit with `time_stop` after 32 bars is the base's behaviour and is exercised through the candle lab run, not by a rule level test.
