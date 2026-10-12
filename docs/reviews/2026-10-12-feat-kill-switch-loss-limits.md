# Review, feat/kill-switch-loss-limits, 2026-10-12

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 62 files, branch vs main (untracked tooling files skipped)
**Verdict**: Approve with nits

## Summary
The change adds a stop, flatten and resume path on the trader, a halt key in Redis that survives restarts, pure loss limit rules in core, and a Postgres seeded actor that halts the trader at a daily or total loss. The dashboard gets the new buttons, a confirm step and the HALTED badge. The design follows spec 0010 closely. The halt is applied in memory before any I/O, flatten goes through each strategy's own exit loop, and the stale tag problem is cleared on every path I traced. I found no blockers and no majors, only edge cases that are narrow or only affect the lab strategy.

## Minor
### 🟡 Resume only checks open positions, not entry orders, `packages/core/src/trading_core/node/control.py:341`
**Problem**: `_resume_refusal` uses `positions_open()` and the `flattening` counter. The rest of the kill switch uses `is_flat()`, which also counts entry orders that are open, in flight or pending cancel. For an automatic halt, `flattening` only turns true once `_wait_flat` starts, which is after the halt key write has been awaited.
**Why it matters**: After a `flatten_timeout` with a stuck entry order, or in the short window after an automatic halt, Resume can succeed while an entry is still alive and could fill after the halt is lifted.
**Suggested fix**: Use `is_flat()` in the refusal, and raise the `_flattening` count in `halt_automatic` before the task is spawned.

### 🟡 A position closed in the first second after midnight is dropped from today, `packages/core/src/trading_core/node/loss_limits.py:225`
**Problem**: `_fold` runs before `roll`. Rolling only happens inside `_evaluate`, so an event that arrives after midnight but before the next tick is folded into the old day. `on_closed` compares the close day with `tally.day`, and `on_opened` compares the open day with `tally.day`. The next roll then zeroes today's figures.
**Why it matters**: A loss realized just after midnight can be left out of the new day's loss, and a trade opened then is not counted for the cap. The window is up to one second.
**Suggested fix**: Call `roll(tally, now)` at the start of `_fold` handling, under the lock, before folding the event.

### 🟡 Resume can race with an automatic halt, `packages/core/src/trading_core/node/control.py:319`
**Problem**: The refusal checks run, then `await delete(...)` yields, then `unhalt()` runs. If the 1 s limit timer trips during that await, `halt_automatic` finds the gate already halted, does nothing new, and marks the limit as tripped. Resume then lifts the halt.
**Why it matters**: A total loss trip in that one Redis round trip is lost for the process, since the total only trips once. Narrow, but it breaks the "total limit needs a config change" promise.
**Suggested fix**: After the delete, re-run the refusal check (or compare the halt id taken before the await) and keep the halt if it changed.

### 🟡 Manual stop on a trader that started with an unreadable key writes an unreadable key, `packages/core/src/trading_core/node/control.py:389`
**Problem**: When the start state is `unknown`, a later stop reuses the existing halt state (`current = self._gate.halt_state or state`) and writes it. `decode` rejects reason `unknown`. No `halt` event is written either, because the halt is not new.
**Why it matters**: The bad key is replaced by another bad key, so every restart logs `halt_key_invalid` again and the audit trail has no manual stop. It still fails closed.
**Suggested fix**: When the held state is `unknown`, replace it with the real stop state before writing, and record the event.

### 🟡 RandomTaker writes an order_rejected event when the kill switch cancels its entry, `packages/core/src/trading_core/node/random_taker.py:235`
**Problem**: `_refused` treats every cancel of the entry as a warning risk event. AC-1 says kill switch cancels are INFO only and raise no `order_rejected`.
**Why it matters**: Lab runs show false rejection events after each stop. The placeholder strategy does this right.
**Suggested fix**: Skip the event when the gate is halted or a flatten was requested, as the placeholder does.

### 🟡 compose.yaml gives RISK_TOTAL_LOSS_SINCE a fallback date, `compose.yaml:198`
**Problem**: `${RISK_TOTAL_LOSS_SINCE:-2026-10-12}` means the "trader refuses to start without it" rule can never fire through Compose.
**Why it matters**: On the VPS a missing variable silently uses a laptop date, and the total limit counts from the wrong day.
**Suggested fix**: Drop the fallback for the VPS file, or require the variable with `:?`.

### 🟡 halt_automatic defers flatten into a task, `packages/core/src/trading_core/node/control.py:363`
**Problem**: The flatten and key write run in a spawned task. If `_spawn` raises, the limit is already marked tripped, so it never retries, and the exception escapes the timer callback before blocks are applied.
**Why it matters**: The trader stays halted in memory, but no flatten starts. Unlikely, since the loop is bound at start.
**Suggested fix**: Call `_flatten_all` synchronously inside `halt_automatic` right after `_apply_halt`, then spawn only the I/O.

## Nits
- ⚪ `packages/core/src/trading_core/node/control.py:356`, the restart flatten uses a fresh UUID for its tags instead of the halt key's `halt_id`, so the orders cannot be matched to the halt.
- ⚪ `packages/core/src/trading_core/node/control.py:290`, the flat timeout in `stop` starts after the key write returns, not when execution started as AC-3 says.
- ⚪ `packages/core/src/trading_core/node/control.py:491`, `int(timestamp() * 1_000_000_000)` goes through a float and loses nanosecond precision; use `clock.timestamp_ns()` style integers.
- ⚪ `packages/core/src/trading_core/node/loss_limits.py:299`, `_event` always logs at WARNING whatever the severity.
- ⚪ `packages/core/src/trading_core/node/loss_limits.py:260`, unrealized P&L adds `Money` amounts without checking the currency is USD.
- ⚪ `packages/core/src/trading_core/node/control.py:500`, any source can send `resume`; consider limiting it to dashboard and cli.

## Strengths
- The halt is applied in memory before any I/O, and the flatten starts before the key write is awaited, so a slow Redis cannot delay the close or leave the trader less halted.
- One `apply_trading_state` owns the risk engine state, so a deploy's unpause cannot lift a halt. A halt maps to REDUCING, not HALTED, so the close itself is not blocked.
- The loss rules are pure and keyed by `(position_ref, opened_ns)`, which handles NETTING and the seed plus live overlap. All money is Decimal and times are aware UTC.
- Fail closed paths are covered: unreadable halt key, Postgres down at start (`loss_unknown`), key write failure, mistagged orders in the mapper.
- The dashboard confirm step is rendered by the server, works without JavaScript, and Stop stays one tap behind session and CSRF checks. The dashboard still only writes to the control stream.

## Test coverage
Coverage is broad: pure limits tests (edges, midnight, duplicates, same position id), kill switch and node control tests, placeholder and random taker flatten tests, sandbox hold drill tests, settings validation, the Postgres seed integration test, and dashboard pause, blocked and CLI tests. Gaps I would add: resume while an entry order is in flight with no position, an event folded just after midnight before the next tick, a limit trip during the resume delete, and a manual stop after an unreadable halt key.
