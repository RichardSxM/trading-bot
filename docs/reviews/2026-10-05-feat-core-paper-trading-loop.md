# Review, feat/core-paper-trading-loop, 2026-10-05

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 75 files, branch vs main (merge base 12228fb, HEAD 7bfb878)
**Verdict**: Changes requested

## Summary
This branch builds spec 0006, the paper trading loop. It adds the `placeholder_timed` strategy on a fee aware sandbox execution client, a pure risk check, a threaded recording writer with spool and dead letter files, `close_abandoned` at start, and the dashboard position card, blocked badge and Trades page. The code is clean, follows the architecture rules (pure rules in `risk/` and `strategy/`, thin Nautilus classes, no Nautilus import in the dashboard, money as Decimal, UTC ints) and is well tested. The one real gap is that the strategy only handles denied and rejected entries, so some other entry endings can leave it stuck or leave a position without a stop.

## Major
### 🟠 Entry ending as canceled or expired is not handled, `packages/core/src/trading_core/node/placeholder.py:307`
**Problem**: The strategy leaves `ENTERING` only on a denied or rejected entry (`_refused`), on a complete fill (`on_order_filled`), or on `PositionClosed`. There is no handler for `OrderCanceled` or `OrderExpired` on the entry. Two cases follow. (1) The entry ends canceled or expired with no fill: `phase` stays `ENTERING` forever, and `_on_boundary` then skips every boundary as "a round trip is open" until the trader restarts. (2) The entry fills partly and is then canceled by the venue: `_on_partial_timeout` returns early because the status is no longer `PARTIALLY_FILLED` (line 391), so no stop is placed and no exit loop starts. The position then has no stop until the hold timer closes it.
**Why it matters**: Rule: anything touching orders fails closed. Case 2 is an unprotected position for up to the hold time. Case 1 is silent loss of all trading with no risk event or block. Sandbox market orders rarely end this way, but the same strategy is the base for real venues later.
**Suggested fix**: Handle the entry's terminal events. On cancel or expiry with no fill, write a risk event and go back to flat. With a partial fill, treat it like the partial timeout: start the exit loop and write the event. Add a test for each.

## Minor
### 🟡 Risk check gets fixed inputs for position and open notional, `placeholder.py:215`
**Problem**: `open_notional=Decimal(0)` and `has_position=False` are hard coded, so the `position_open` and the open notional part of `leverage_cap` can never fire. The guard sits in `_on_boundary` instead.
**Why it matters**: The pure check looks like a second line of defence but is not one for these two rules. A later strategy that reuses `_decide_entry` loses them silently.
**Suggested fix**: Pass the real values from the cache position, or say in the docstring that the caller guarantees flat.

### 🟡 A failed submit leaves the strategy in `ENTERING`, `placeholder.py:260`
**Problem**: `phase`, `_entry_decision` and `_entry_id` are set before `self.submit_order(order)`. If it raises, `_on_boundary` logs and swallows it, but the phase stays `ENTERING` and later boundaries skip.
**Why it matters**: Same stuck state as the major, with only a log line.
**Suggested fix**: Set the phase after a successful submit, or reset to flat in the except path, and write a risk event.

### 🟡 Hold timer firing during the exit loop records an orphan decision, `placeholder.py:406`
**Problem**: When the stop failed first, the exit loop already runs under the entry's decision. `_on_hold_over` then writes a new `exit` decision, but `_start_exit_loop` returns early, so no order carries it. A time exit after a failed stop is also reported as `stop_failure` by `exit_reason` (the market fill carries the entry decision), and so is the partial entry exit.
**Why it matters**: The decision journal has an approved exit with no order, and the Trades page labels can mislead.
**Suggested fix**: Skip the decision when already `EXITING`, or link it to the running exit order. Decide on a separate reason for the partial entry exit.

### 🟡 A bad spool file blocks every start, `packages/core/src/trading_core/recording/spool.py:176`
**Problem**: `read` raises `SpoolDecodeError` on any bad complete line, and `replay_spool` does not catch it, so `start_paper_loop` fails and the trader exits 1 on every restart.
**Why it matters**: Failing closed is right, but the way out is to find and edit the file by hand on the volume, with no log line naming it.
**Suggested fix**: Log the file name with a clear code. Consider moving a file that cannot be decoded to a `bad-` name and blocking entries instead of refusing to start.

### 🟡 Dead letter write can kill the worker thread, `packages/core/src/trading_core/recording/writer.py:91`
**Problem**: `append(deadletter, [item])` runs inside the except handlers of `write_one`. A disk error there (full volume, permissions) raises out of `write_one` and ends the `_run` loop, with no `recording_failed` block.
**Why it matters**: Writes stop. The backlog age check does eventually raise `recording_unhealthy`, so entries do block, but the cause is hidden and the thread never recovers.
**Suggested fix**: Wrap the dead letter append, add `recording_failed` to the blocks and log it if it fails.

## Nits
- ⚪ `packages/core/src/trading_core/node/paper_snapshots.py:66`, `str(_cents(...))` gives the text "None" for a missing average; the dashboard then marks the card `bad`. Send `None` instead.
- ⚪ `packages/core/src/trading_core/node/sandbox.py:120`, renaming the factory class to `SandboxLiveExecClientFactory` depends on a Nautilus 1.231 internal. It is commented and the version is pinned; consider a test that fails on upgrade (check one exists).
- ⚪ `apps/dashboard/src/dashboard/web/page_routes.py:142`, `page` has no upper bound; a huge value reaches Postgres and only shows the error state. Add `le=` to the Query.

## Strengths
- Pure rules (`risk/check.py`, `risk/sizing.py`, `strategy/placeholder.py`, `strategy/blocks.py`) have no I/O, full docstrings and clear first refusal ordering that matches AC-5.
- The recording path is careful: bounded queue that never blocks the event loop, transient vs conflict vs poison item classes, dead letter and spool files, a drain budget checked against the Compose stop grace, and a monotonic `last_event_ns` fix for mixed clocks.
- Fails closed in the right places: env guard allowlist for the sandbox, the drill switch limited to demo, the demo only cache purge scoped by key pattern with SCAN and UNLINK, and `close_abandoned` is idempotent.
- The dashboard stays read only apart from the control stream, shows `offline` rather than `flat` for a dead trader, and reads P&L off the event loop with a timeout.

## Test coverage
Strong. Core has unit tests for the strategy node, risk, sizing, strategy rules, writer, spool, sandbox, settings and env guard, plus Postgres integration tests for `close_abandoned`, paper recording and the Trades queries. The trader and dashboard have tests for the purge, smoke path, position card, blocked badge and Trades page. Gaps: no test for an entry that is canceled or expired (with or without a partial fill), a submit that raises, a corrupt spool file, or a dead letter write failure.
