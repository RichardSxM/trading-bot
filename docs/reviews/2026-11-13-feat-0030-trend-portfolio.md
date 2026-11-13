# Review, feat/0030-verify-vps (feature 31, spec 0030 trend portfolio), 2026-11-13

**Reviewed by**: claude-fable-5-2 (author model not stated)
**Scope**: 81 files, feature range c2a4f1c..HEAD (PRs #134, #135, #136 plus local docs and test commits), `uv.lock` and the two `context_feed` files left out
**Verdict**: Changes requested

## Summary

The change adds the `trend_portfolio` rule, its risk check, one multi coin node, a trader slot that lets a second demo trader run beside six vote, a migration, alerter and deploy support, and a Portfolio page. The pure parts (rule, risk check, slots) are clean, exact, and well tested, and the 10x demo settings cannot reach live: both the slot and the config builder refuse outside demo. Two things should be fixed before this is called done. A kill switch close that is rejected or left short is never sent again, and almost every log based alert rule only matches the service `trader`, so the portfolio trader's critical lines (a flatten timeout among them) reach nobody. Both sit on the safety path of a book that runs at about 10x with no stops.

## Major

### 🟠 A rejected or short kill switch close is never retried, `packages/core/src/trading_core/node/trend_portfolio.py:277`

**Problem**: `flatten` and `flatten_for` add each coin to `_closing` and skip any coin already in it. The only place a coin leaves `_closing` is `on_position_closed` (line 650). The close orders are sent without `track`, so `_order_refused` returns at once for them (line 657), and `_drop_round` keeps `_closing` on purpose (line 702). So if the close is rejected, denied, cancelled, or expires, or if a grow that was already in flight fills after the close was sized and leaves a rest, the position stays open and the coin stays in `_closing`. `KillSwitch._wait_flat` calls `flatten_for` again on every poll (`node/control.py:540`), and the base class says `flatten` "is called again on every poll" so it must be safe to repeat, but here every repeat skips the coin. It stays stuck for later stops too, until the process restarts.
**Why it matters**: The stop button and the loss limit halt are the only guards this strategy has (no stops, about 10x gross). One rejected close leaves a position open through the whole flat timeout and after it, and the trader reports "not flat" with no further attempt. The sandbox already has a drill for exactly this (`SANDBOX_REJECT_KILL_SWITCH_ORDERS`), and the other strategies retry their exit. This breaks the "anything touching orders fails closed" rule.
**Suggested fix**: Track the flatten orders, and drop the coin from `_closing` when its close order is rejected, denied, cancelled, expired, or filled while the position is still open, so the next poll sends a fresh reduce only close for what the cache holds. Or drop `_closing` and skip a coin only while a reduce only order for it is still open or in flight in the cache. Add a node test: reject the kill switch close once, call `flatten` again, and expect a second close; and one where a grow fills after the first close.

### 🟠 Log alert rules only match the service `trader`, so the portfolio trader's critical lines are silent, `packages/core/src/trading_core/alerts/rules.py:155`

**Problem**: `_TRADER_ONLY = frozenset({"trader"})`, and `_risk_critical` (line 143) and `_engine_error` (line 147) test `obs.service == "trader"`. The second trader's container is the compose service `trader-portfolio`, so none of these rules match its lines: `loss_limit_halt`, `risk_critical` (every CRITICAL line with an `event_key`, such as `flatten_timeout`), `risk_halt_manual`, `trader_critical_codes` (`positions_abandoned`, `kill_switch_task_crashed`, `halt_key_invalid`, `record_conflict`), `snapshot_write_failed`, `stale_feed`, `warmup_seed_failed`, `fill`, and engine `[ERROR]` lines. The alerter work in this change covers only the heartbeat, the entry block check, and the failed coins. This confirms the gap named in the review brief.
**Why it matters**: The spec's user story is "safety does not get weaker with two traders". Today a loss limit halt, a kill switch that could not get flat, or a record conflict on the portfolio trader sends no push. The entry block check would catch a halt only after an hour, and catches a flatten timeout not at all. Spec AC-17 did not ask for these rules, so this is a gap in the design as much as in the code, but it is real on the VPS now.
**Suggested fix**: Let the trader rules match both services (for example a `TRADER_SERVICES` set holding `trader` and `trader-portfolio`), and put the service or slot into the fingerprint and the alert text so the two traders' episodes stay apart. Decide on purpose whether `fill` pushes should stay off for the portfolio (about 12 a week) and say so in a comment. Add promise rows or tests with `service="trader-portfolio"` for at least `loss_limit_halt`, `risk_critical`, and `trader_critical_codes`. Amend spec 0030 AC-17 through /architect to record the choice.

## Minor

### 🟡 A restart inside the start cooldown leaves the book flat until Monday, with no alert, `packages/core/src/trading_core/node/trend_portfolio.py:357`

**Problem**: On demo every start is flat. When a `start` decision row is younger than `start_cooldown_s`, the start rebalance is skipped and nothing tries again once the hour has passed: the daily run only plans, and only Monday rebalances. Held rows count as well (they are written with reason `start`). The spec's Consequences line says the cooldown "bounds a crash loop to one rebuild an hour", which reads as if the rebuild comes later; the code never does it.
**Why it matters**: The traders were memory killed twice on the VPS this month. A second restart within an hour leaves the forward test with no positions for up to a week, and only an INFO line says so. Flat is safe, but the test result is quietly wrong.
**Suggested fix**: When the start is skipped for the cooldown, arm one timer for the end of the cooldown that runs the start rebalance then (if still flat and not blocked). Or at least log it at WARNING and show it on the Portfolio page.

### 🟡 A crash inside a rebalance is logged and nothing else, `packages/core/src/trading_core/node/trend_portfolio.py:334`

**Problem**: `_on_daily`, `_on_start_poll`, `_on_gate_poll`, and `_on_retry` catch `Exception` and log `portfolio_run_failed`. `_last_weekly` is set before `_rebalance` runs (line 332), so a Monday run that throws half way (a missing instrument raises `LookupError` in `_step`) is not tried again that week. Coins after the failing one are never stepped, `_maybe_send_grows` is not reached, `failed` stays empty, so the failed coins alert does not fire, and there is no alert rule for `portfolio_run_failed`.
**Why it matters**: A half done weekly rebalance (reductions sent, grows not) looks healthy on every surface. The project rule is "never swallow" an error.
**Suggested fix**: On an exception, put every coin not yet handled into `failed` (so the existing alert fires), still call `_maybe_send_grows` for what was collected, and add `portfolio_run_failed` to a WARNING alert rule. Per coin `try` inside the loop would keep one bad coin from stopping the rest.

### 🟡 A grow held by an entry block (not a pause or halt) is never sent later, `packages/core/src/trading_core/node/trend_portfolio.py:547`

**Problem**: `_send_grow` holds a grow for any active block (`trade_cap`, `recording_unhealthy`, `stale_feed`, `loss_cooldown`, and so on), but `_on_gate_poll` only watches `paused` and `halted`. When such a block clears, nothing rebalances. For a flip the close has already gone, so the coin sits flat until next Monday. The coin is not put in `failed`, so no alert.
**Why it matters**: A short database hiccup at 00:05 on a Monday can drop the open leg of every flip for a week without any sign beyond an INFO line and a refused decision row.
**Suggested fix**: Treat self clearing blocks like `order_unresolved` (retry after `retry_wait_s`, then `failed`), or include "any block active" in what the gate poll watches so a clear triggers the `resume` rebalance.

### 🟡 The failed coins alert can name only the first coin that failed, `apps/alerter/src/alerter/service.py:201`

**Problem**: `check_portfolio_failed` fires once per `last_rebalance_at`. Coins reach `failed` at different times (a liquidation distance refusal at once, retries after up to three minutes). The first poll that sees a non empty list sets `_failed_seen`, and coins that fail later in the same rebalance are never reported.
**Why it matters**: The push says "1 coin failed" when four did.
**Suggested fix**: Dedupe on `(last_rebalance_at, tuple(failed))`, or wait until `retry_max x retry_wait_s` has passed since `last_rebalance_at` before sending.

### 🟡 One decision row per coin hides what happened to the second leg and to retries, `packages/core/src/trading_core/node/trend_portfolio.py:746`

**Problem**: `_decision` writes once per coin per rebalance. For a flip the row is the approved close (`ADJUST`); if the open leg is then refused (liquidation distance, held) or clipped, nothing is recorded. For a grow whose first try was refused the row says `REJECTED`, and a retry that later fills carries that same decision tag, so fills link to a rejected decision.
**Why it matters**: AC-7 wants the outcome and any clip in the decision row. The journal and any later study of the forward test will read wrong outcomes for these cases.
**Suggested fix**: Give the flip's open leg its own decision id and row, and write a new row (new id) when a retry gets a different verdict than the first try.

### 🟡 The migration says the previous release still works on the new schema, but its insert does not, `packages/core/src/trading_core/migrations/versions/20261107_0015_command_slot.py:5`

**Problem**: The previous release records a command with `ON CONFLICT (id) DO NOTHING` (`_insert_or_compare(..., ["id"])`). After this migration the only unique constraint is `(id, slot)`, and Postgres refuses `ON CONFLICT (id)` without a matching unique index. So after a rollback to a release from before this change, every command record write fails.
**Why it matters**: Commands still run (the consumer logs a Postgres error and goes on), so it is not dangerous, but the rolled back trader records no commands and loses its duplicate check, and the docstring tells the next reader the opposite.
**Suggested fix**: Correct the docstring. A unique index on `id` alone cannot coexist with two slots, so the old insert cannot be kept working; say in the docstring and the deploy notes that a rollback past 0015 records no commands until the downgrade is run.

### 🟡 Missing tests on the node's riskier branches, `packages/core/tests/test_node_trend_portfolio.py`

**Problem**: No test covers a rejected or short kill switch close (the first Major), the `tp_grows_fallback` timer (grows sent when a reduction gets no answer), the `order_unresolved` deferral, a grow that turns into a reduction at the new mid, the `equity_unknown` and `plan_empty` paths, or an exception inside `_rebalance`.
**Why it matters**: These are the branches where the state machine can stall, and they touch orders.
**Suggested fix**: Hand these to /test once the two Majors are fixed.

## Nits

- ⚪ `packages/core/src/trading_core/node/trend_portfolio.py:525`, the side for the refused row compares `target_notional` (USD) with `current` (coins); compare the target's sign with the current quantity, or size from the last known mid.
- ⚪ `packages/core/src/trading_core/node/trend_portfolio.py:535`, a grow that became a reduction at the new mid is logged as `rebalance_skip_small`; give it its own code so the log tells the truth.
- ⚪ `packages/core/src/trading_core/node/trend_portfolio.py:454`, `assert self.plan is not None` in order code; pass the plan in or raise a typed error.
- ⚪ `apps/dashboard/src/dashboard/web/app.py:268`, `read_portfolio` makes seven Redis round trips in a row on every tick, inside the same timeout as the Overview reads; one `MGET` plus the two stream reads would do.
- ⚪ `packages/core/src/trading_core/control/consumer.py:52`, check whether `GROUP` and `CONSUMER` still have a reader now that the consumer uses the slot's names; drop them if not.

## Strengths

- The 10x demo values cannot reach live unreviewed: `TRADER_SLOT=portfolio` refuses outside demo, `trend_portfolio_config` calls `_demo_only`, the rule and node defaults stay at the tested 0.02 and 2, the values are part of the version hash (so a change needs a new promotion), and tests pin all of it.
- `strategy/trend_portfolio.py` and `risk/rebalance.py` are pure, all `Decimal`, typed errors, validated params, and read exactly like AC-1, AC-2, and AC-5.
- `control/slots.py` keeps `main` byte for byte as before by calling the old functions, and a test pins every `main` name; the new group starts at `$` so no old `stop` is replayed.
- Slot scoped `risk_events` keys, `loss_seed` by `trader_id`, and the `(id, slot)` command key are the right fixes for two traders reading one command stream, with integration tests against real Postgres.
- The node tracks an order before `submit_order` so a denial that arrives inside the call is still retried, and counts in flight grows in the exposure the check sees.
- The alerter keeps each slot's episodes apart by subject and names the slot in the title; `main`'s text is unchanged.

## Test coverage

Good on the pure code: the rule (hand computed weights, scaling, eligibility, zero vol, bad closes), `order_delta`, and every branch of `check_rebalance`. The node has about 25 tests for the start, weekly, resume, flip, clip, hold, retry, cooldown, missing close, seed, and flatten during a flip. Slots, the consumer, the CLI `--slot` flag, the migration (up and down), `loss_seed` by trader, the journal column, the alerter slots, the dashboard page, and the deploy script dry run all have tests, and the integration ones use real Postgres. Not covered: a rejected or short kill switch close, the grows fallback timer, the `order_unresolved` deferral, a held grow after a non pause block clears, the cooldown skip followed by time passing, exceptions inside a rebalance, and any alert rule fed a `trader-portfolio` log line. The first and the last of these would have caught the two Majors.
