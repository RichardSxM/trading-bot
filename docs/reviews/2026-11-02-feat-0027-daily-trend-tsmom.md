# Review, feat/0027-daily-trend-tsmom (merged, PR #119 and #120), 2026-11-02

**Reviewed by**: Sonnet 5.5 (author on Sonnet)
**Scope**: 31 files, branch range 753ec82^..57c0fce vs main history
**Verdict**: Approve with nits

## Summary
The change adds a `trend` strategy kind to the gate (thresholds v3, long only check, prior trials in N), a pure `tsmom` rule with a thin Nautilus wrapper, a warmup pre roll in the candle lab, and migration 0014. Every new gate branch is keyed on the trend kind, v1 and v2 thresholds still round trip, and the pre roll is guarded by a hard failure if it ever trades. No blockers or majors. A few minors are listed below.

## Minor
### 🟡 Late open closer is on for every candle unit, `apps/lab/src/lab/candle_units.py:337`
**Problem**: `close_late_opens=True` is passed for all candle runs, not only the trend kind. Scalping and hour level runs that used to fail with "a position is open after the range end close" now get closed silently instead.
**Why it matters**: The spec says scalping and hour level behavior never changes. Only runs that used to fail are affected, so past verdicts are safe, but the failure that flagged the edge case is gone for those kinds.
**Suggested fix**: Key it on the strategy kind, or record a risk event or log line when a late open is closed so the case stays visible.

### 🟡 Late open closer has no unsubscribe and no double close guard, `apps/lab/src/lab/actors.py:340`
**Problem**: `_on_position_opened` subscribes to `events.position.*` and never unsubscribes. Nothing stops a second `_close` call for the same position if two PositionOpened events arrive after the end.
**Why it matters**: Harmless in a backtest that disposes the engine, but a second close order on one position would be refused or flip the book.
**Suggested fix**: Remember the closed position id, or unsubscribe in `on_stop`.

### 🟡 Pre roll start only searches the first stored month, `packages/core/src/trading_core/lab/pre_roll.py:26`
**Problem**: The loop skips leading months with no candles but a gap in the middle of the pre roll is not seen. The stream then feeds a hole into the daily bars.
**Why it matters**: A gap would make the lagged close and ATR come from the wrong days, silently. The pinned data is complete today, so this is a latent risk.
**Suggested fix**: Fail closed (typed error) when a month between the pre roll start and the segment start is missing after the first stored one.

### 🟡 Stderr "--- Logging error ---" blocks in lab tests, `packages/core/src/trading_core/logs.py:42`
**Problem**: `configure_logging` installs a `StreamHandler(sys.stdout)`. When a test calls it under pytest capture, the handler keeps a reference to the capture stream. After that stream closes, any later log record (for example from a Nautilus engine dispose) prints a logging error. I read the code only and did not run the tests, so this is the likely cause, not a proven one.
**Does this spec contribute?** Only in volume. The pre roll makes `fresh_signal` log `entry_skipped_stale_signal` for each dropped signal (`packages/core/src/trading_core/node/strategy.py:325`), so more records hit the dead handler. The cause is older than this spec and is not in its diff.
**Suggested fix**: In tests, reset root handlers in a fixture teardown, or have `configure_logging` use a handler that looks up `sys.stdout` at emit time.

## Nits
- ⚪ `apps/lab/src/lab/candle_units.py:224`, `_keep_segment_rows` checks decisions, order events, fills and positions but not the `orders` rows themselves; the order events cover it in practice.
- ⚪ `packages/core/src/trading_core/gate/thresholds.py:509`, `_ZERO_OK` is matched on the `where` string `"trend.prior_trials"`, which is brittle if the message path format changes (a test covers it today).
- ⚪ `packages/core/src/trading_core/node/tsmom.py:725`, the `long_only` field can only ever be true, so it is a declaration, not a setting; fine, but the docstring could say the gate reads it from the frozen config.

## Strengths
- Gate invariants hold: every new branch is keyed on `strategy_kind == trend`, direction and top trades shares are still recorded when not applied, `long_only_violated` fails closed on missing measures, and a byte identical v2 round trip test exists.
- The pre roll is safe by construction: signals before the segment are dropped via `signals_after`, the recorder ignores pre roll bars, and `_keep_segment_rows` raises if anything traded early.
- Migration 0014 is expand only and its downgrade refuses while `trend` rows exist; there is an integration test.
- The pure rule in `strategy/tsmom.py` has docstrings, uses Decimal, and holds no state beyond the close window.

## Test coverage
Good. Core has tests for the rule, node config, thresholds (v1, v2, v3), verdict branches and the migration. Lab has tests for the pre roll, nothing before segment start, the range end late open case (`apps/lab/tests/test_tsmom.py:226`), the registry, the run file, the SOL and XRP pins, and cut invariance. Gate has check version tests for kind trend. Not covered: a gap in the middle of a pre roll, and a double late open close.
