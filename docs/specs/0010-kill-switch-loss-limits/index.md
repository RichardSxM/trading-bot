# 0010. Halt, cancel, and flatten on one stop, and halt by itself at a daily or total loss limit

**Date**: 2026-10-12
**Status**: In Progress

## Summary

One tap on the red stop button now really stops the bot. It blocks new entries, cancels entry orders, closes every position with a market order that can only shrink it, and stays halted until you press Resume. A halt survives a restart.

The trader also halts and flattens by itself when today's loss reaches 20 USD or the loss since a start date reaches 100 USD. The daily halt keeps entries blocked until UTC midnight, even after Resume. The total halt needs a config change before Resume works again.

Two softer guards only block new entries: a cap of 50 trades a day, and a 10 minute cooldown after any losing trade. Nothing new goes into Postgres. The halt lives in a Redis key next to the pause key, and the loss numbers are read from the `positions` table at start and then kept current from NautilusTrader's position events.

## Requirements

**User stories**:
- As the trader's owner, I want one tap to stop everything and leave me flat, so a panic on my phone is never slower than the market.
- As the owner, I want the bot to stop itself after a bad day or a bad run, so a bug or a broken strategy cannot drain the account while I sleep.
- As the owner, I want a way to close a position without stopping the day, so I can exit one bad trade and let the strategy carry on.
- As the owner, I want guards against overtrading and revenge trading, so fees and tilt (trading worse after a loss) cannot quietly eat the account.
- As the owner, I want to see why the bot is halted or blocked, and how close it is to each limit, without reading logs.

**Acceptance criteria** (the contract, each criterion is IDed and independently checkable):

*Stop, flatten, resume*

- **AC-1** (stop): a `stop` command first halts in memory. The gate refuses every order that is not reduce only, and the risk engine goes to `REDUCING`. Then, without waiting on one another:
  - **Write the halt key** (AC-5). This runs at the same time as the two steps below, so a slow Redis never delays the close.
  - **Cancel entries.** Cancel every open and in flight entry order (orders that are not reduce only) of every strategy.
  - **Close positions.** Close every open position with a reduce only market order, GTC, through the strategy's existing exit loop (spec 0006 AC-8a), retried every 1 s on reject, deny, cancel, or expire.
    - **Tags.** Every order of that loop, retries included, is tagged `origin:kill_switch` and `halt:<halt_id>`. If an exit loop was already running (a time exit, a stop failure, a partial entry), the flatten adopts it, and its next retries carry the tags.
    - **Exit failed.** After 5 refusals, the `exit_failed` block and event apply as in spec 0006.
    - **The record.** `orders.origin` is `kill_switch`, `orders.decision_id` is null, and the Trades page shows the exit reason as `kill switch`.

  The protective stop stays in place until the position is closed. The existing `on_position_closed` then cancels it. Cancels of entry orders while halted or flattening are expected: they are logged at INFO and raise no `order_rejected` event. The trader stays halted until a `resume` succeeds (AC-4).
- **AC-2** (flatten): a `flatten` command does steps 3 and 4 of AC-1, but does not halt and does not change the pause. A strategy may enter again on its next signal once flat.
- **AC-3** (when stop and flatten answer):
  - **`executed`** only once every strategy has no open position and no open or in flight entry order.
  - **`failed` with reason `flatten_timeout`** if that has not happened `KILL_SWITCH_FLAT_TIMEOUT_S` (20) after the trader started executing the command. The trader writes one `critical` `order_rejected` risk event keyed `flatten_timeout:<halt_id>`, and the exit retries keep going.
  - **A late fill.** An entry that fills after the halt (it was in flight when stop arrived) is flattened the same way within the same wait. A strategy asked to flatten while it has no position remembers the request. If its entry then fills, it places the protective stop as usual and starts the exit loop at once.
  - **Flat.** "Flat" means no open position and no entry order that is open, in flight, or pending cancel. Reduce only orders don't count.
  - **A second command.** The consumer runs commands one at a time. A `stop` or `flatten` sent while one is running waits its turn, then does the same work again, which is idempotent (safe to repeat).
  - **An automatic halt** (AC-9, AC-10) has no command to answer. Its flat wait runs as its own task keyed by its `halt_id`, writing the same `flatten_timeout` event on timeout.
- **AC-4** (resume): a `resume` command deletes both `{env}:trader:halt` and `{env}:trader:pause` in one `DEL`, then lifts the halt and the pause in memory, and sets the risk engine to `ACTIVE`. It writes one `info` `resume` risk event keyed `resume:<command id>`. It answers `failed`, changing nothing, with:
  - `not_flat`, while a stop's flatten is still running or the trader is halted with a position still open
  - `total_loss_limit`, while the total loss (AC-8) is at or over `RISK_TOTAL_LOSS_LIMIT_USD`
  - `loss_unknown`, while the loss figures have not been seeded yet (AC-13), since a limit that cannot be measured cannot be cleared
  - `halt_key_delete_failed`, when the `DEL` fails (the trader stays halted and paused)

  A resume on a trader that is not halted still clears any pause and answers `executed`.

  Resume never clears an entry block. The `daily_loss_limit`, `trade_cap`, `loss_cooldown`, and `loss_unknown` blocks hold until their own rule clears them.
- **AC-5** (halt key): the halt lives in `{env}:trader:halt`.
  - **The value.** A JSON object with `reason` (`manual_stop`, `daily_loss_limit`, or `total_loss_limit`), `source` (`dashboard`, `cli`, or `trader` for an automatic halt), `halted_at` (ISO 8601 UTC with `Z`), and `halt_id` (UUID: the command id for a command, a new UUID7 for an automatic halt).
  - **No TTL.** The key has no expiry and lives in Redis's AOF.
  - **At start.** The trader reads the key before any strategy runs:
    - absent: it runs;
    - present: it starts halted;
    - unreadable or malformed: it starts halted in memory with reason `unknown` and one `critical` log `halt_key_invalid` (fail closed).
  - **Already halted.** A second halt (a later stop, or a limit tripping while halted) leaves the existing key as it is and writes no second `halt` event. A later stop still flattens again.
- **AC-6** (halt key write fails): if the key cannot be written, the trader still halts in memory and flattens. It answers `failed` with `halt_key_write_failed` and writes one `critical` `halt` risk event keyed `halt_key_write_failed:<halt_id>`. A restart before the next successful halt would come back running, and the dashboard's "Stop failed" bar says so.
- **AC-7** (halt record): every new halt writes one `halt` risk event keyed `halt:<halt_id>`, carrying `details` = `{reason, source}`.
  - Severity: `warning` for `manual_stop`, `critical` for an automatic halt.
  - **After a restart.** A trader that starts halted flattens again 5 s after start (a one time alert set in `ControlActor.on_start`), once its strategies are running. This is a no op when flat. It covers a restart or SIGTERM in the middle of a flatten.
  - **Where events go.** Events go to the paper loop's recording queue (demo). In live, with no run yet, they are logged only, until the first live strategy wires a writer.

*Loss limits and entry guards*

- **AC-8** (what counts as a loss): the trader keeps three loss figures in USD.
  - **Today's loss:** minus the sum of three things: `positions.realized_pnl` of positions closed since UTC midnight (net of fees, including positions closed as abandoned at a demo restart), the realized P&L of positions closed live since the seed, and the open positions' unrealized P&L at the current mid.
  - **Total loss:** the same sum, over positions closed since `RISK_TOTAL_LOSS_SINCE` (00:00 UTC that day).
  - A profit makes the figure negative (no loss).
  - Funding is not counted yet (feature 10 owns funding in P&L).
  - **Counted once.** A position is identified by `(position_ref, opened_ns)`, the recorder's own key. Under NETTING every round trip reuses the same position id, so the id alone is not enough. Each position counts once.
  - **Every close counts.** A position closed by stop, flatten, or the kill switch counts like any other: it is real money. Its loss also starts the cooldown (AC-12).
- **AC-9** (daily limit): when today's loss is at or over `RISK_DAILY_LOSS_LIMIT_USD`, the trader halts with reason `daily_loss_limit` (AC-1 steps 1 to 4, source `trader`) and blocks entries with `daily_loss_limit`.
  - The block holds until the next UTC midnight, even if the loss shrinks or you resume, and then clears by itself.
  - **One trip per day.** The trip fires once per UTC day: after it, a loss still over the limit does not halt again, so Resume sticks. The total limit's trip likewise fires once per process, and Resume is guarded by AC-4 instead.
  - A restart the same day blocks again when today's realized loss alone is at or over the limit, or when a `halt` risk event with reason `daily_loss_limit` exists since UTC midnight.
- **AC-10** (total limit): when the total loss is at or over `RISK_TOTAL_LOSS_LIMIT_USD`, the trader halts with reason `total_loss_limit`. Resume answers `failed` with `total_loss_limit` (AC-4) until a config change (a higher limit or a later `RISK_TOTAL_LOSS_SINCE`) and a restart bring the loss under the limit. When both limits trip in the same check, the halt reason is `total_loss_limit`.
- **AC-11** (trade cap): when the positions opened since UTC midnight reach `RISK_MAX_TRADES_PER_DAY`, entries are blocked with `trade_cap` until the next UTC midnight. Nothing is flattened. The first time it happens each day, the trader writes one `warning` `limit_hit` risk event keyed `trade_cap:<YYYY-MM-DD>`.
- **AC-12** (cooldown): when a position closes with `realized_pnl` below zero, entries are blocked with `loss_cooldown` until that position's close time plus `RISK_LOSS_COOLDOWN_S`. A later loser restarts the wait. Nothing is flattened. A restart within the wait blocks again, from the newest losing position's `closed_at`. `RISK_LOSS_COOLDOWN_S = 0` turns the cooldown off.
- **AC-13** (where the figures come from):
  - **At start.** After the demo's abandoned positions are closed (spec 0006 AC-15), the trader reads today's and the total realized P&L, today's opened position count, the newest losing `closed_at`, and today's `daily_loss_limit` halt event from Postgres for its env.
  - **Live.** The actor subscribes to position events before the read starts, and holds the events that arrive meanwhile. Once the read lands, it folds them in. From then on, each NautilusTrader `PositionOpened` and `PositionClosed` updates the figures at once.
  - **No double counts.** A position already in the seed (same `(position_ref, opened_ns)`) is not added again, including one the recording writer wrote between the subscription and the read.
  - **Off the event loop.** The read runs in a worker thread (`asyncio.to_thread`), so it never blocks the node's event loop.
  - **Postgres down at start.** Until the read succeeds, entries are blocked with `loss_unknown`. The read retries every 30 s, logging `loss_seed_failed` on each failure.
- **AC-14** (when the limits are checked): on every `PositionOpened` and `PositionClosed`, and on a 1 s timer that runs all the time, flat or not. The timer reads the latest quote from the cache for the unrealized part, rolls the figures over at UTC midnight by the trader's clock, and clears blocks whose time is up.
- **AC-15** (entry check): `check_entry` gains a `halted` input, checked first. The refusal order becomes `halted`, `paused`, `blocked:<reason>`, `position_open`, `zero_quantity`, `notional_cap`, `leverage_cap`. `halted` is an `info` refusal like `paused`.
  - **The base class.** It denies every order that is not reduce only while halted, logging `entry_denied_halted`.
  - **The strategies.** `PlaceholderTimed` and `RandomTaker` pass `halted` into `EntryRequest`, and treat it like `paused` in their own check right before submitting, so they go back to flat instead of waiting on an entry that was never sent.
  - **The dashboard list.** `entries_blocked` lists `halted` (with plain words) while halted, as it lists `paused`.

*What you see and send*

- **AC-16** (snapshot): `trader_state` gains these fields:
  - `halted`, `halt_reason` (`manual_stop`, `daily_loss_limit`, `total_loss_limit`, `unknown`, or null), `halted_at`, `flattening`
  - `loss_today_usd`, `loss_total_usd`, `daily_loss_limit_usd`, `total_loss_limit_usd`
  - `trades_today`, `max_trades_per_day`, `cooldown_until`

  The money values are decimal strings and the times are ISO 8601 UTC. `trading_state` reads `REDUCING` while halted. The loss figures, `trades_today`, and `cooldown_until` are `null` until the seed lands, and the dashboard shows them as `unknown`.
- **AC-17** (dashboard):
  - **Top bar.** Shows a `HALTED` badge with the reason in plain words and its age (`HALTED · daily loss limit · 12m`) on every page, next to the stop button.
  - **System page.** Shows today's loss against the daily limit and the total loss against the total limit, trades today against the cap, and the cooldown time left.
  - **Plain words.** The entries blocked list shows plain words for `daily_loss_limit`, `trade_cap`, `loss_cooldown`, and `loss_unknown`.
- **AC-18** (dashboard buttons):
  - **Flatten.** The System page gains a Flatten button.
  - **Resume.** The existing Resume button now sends `resume` instead of `unpause`.
  - **Confirm step.** Both take a confirm step that works without JavaScript.
  - **Stop.** Stop stays one tap on every page.
  - **Reasons.** A `failed` result shows its reason in plain words in the command state (`resume failed · not flat yet`).
- **AC-19** (command line): `control_cli` gains `stop`, `resume`, and `flatten`, with `--wait-result` and `--ttl` as `pause` has, sent with `source = cli`. `--wait-result` prints the final outcome and reason and exits 0 only on `executed`.
- **AC-20** (deploy unchanged): a deploy's `pause`, `unpause`, and `wait-flat` (spec 0005 AC-8, AC-11) leave the halt key alone. A halted trader comes back halted after a deploy.
- **AC-21** (config): the trader refuses to start when any of these holds:
  - a loss limit is zero or below;
  - `RISK_TOTAL_LOSS_LIMIT_USD` is below `RISK_DAILY_LOSS_LIMIT_USD`;
  - `RISK_TOTAL_LOSS_SINCE` is missing or in the future;
  - `RISK_MAX_TRADES_PER_DAY` is below 1;
  - `RISK_LOSS_COOLDOWN_S` is below 0;
  - `KILL_SWITCH_FLAT_TIMEOUT_S` is outside 5 to 120.

## Decision

**Chosen option**: Option 2: a trader side kill switch over the existing control stream, with pure loss rules in core and a Redis halt key.

The trader owns stop, flatten, and resume through the `ControlActor` it already runs. Flattening goes through each strategy's own exit loop. The loss rules are a pure function in `trading_core.risk`, fed from Postgres at start and from NautilusTrader's position events after that. The halt state lives in a Redis key that mirrors the pause key.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `redis` (`terminalskills/skills`, `.claude/skills/redis/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `htmx` (`.claude/skills/htmx/`) · `fastapi` (`.claude/skills/fastapi/`)

**The calls this spec makes** (each with the runner up):
- **A halt maps to `REDUCING`, never NautilusTrader's `HALTED`.** `HALTED` denies every new order, the reduce only close included, so it would block the very flatten it should protect.
  - **One owner of the engine state.** A single `apply_trading_state()` sets the engine from both flags, through `target_trading_state(paused, halted)`, which returns `REDUCING` when either is set. Both `PauseController` and `KillSwitch` call it. So a deploy's `unpause` on a halted trader keeps `REDUCING`.
  - **The order at start.** The pause key and the halt key are both read, then the state is applied once.
  - **Runner up:** switch to `HALTED` once flat. That adds a second transition to get wrong, for no gain while the gate already refuses entries.
- **Flatten goes through each strategy's exit loop,** through a new `KfsStrategy.flatten(halt_id)` hook.
  - **With a position:** the placeholder sets its kill switch tags (strategy state that lives until `_reset`) and runs `_start_exit_loop`, or adopts the loop already running. Retries, `exit_failed`, and stop cancellation then behave as spec 0006 tested.
  - **Without a position:** it cancels its entry order and sets `kill_switch_pending`. An entry fill that arrives later places the stop, then starts the exit loop.
  - **Runner up:** NautilusTrader's `Strategy.close_all_positions()` called from an actor. Simpler, but it skips the strategy's retry loop and its phase bookkeeping, so a rejected close would be left on its own.
- **One `KillSwitch` object in `node/control.py`** runs stop, flatten, and resume, and is the one place that waits for flat.
  - **Where it gets the strategies.** `main.py` hands it the list after the strategies are created (`node.trader.strategies()`, filtered to `KfsStrategy`).
  - **Live.** With no strategy, flat is judged from the cache alone, and a position there answers `flatten_timeout`.
  - **Pause stays separate.** `PauseController` stays for pause and unpause.
  - **Runner up:** grow `PauseController`. That would mix two state machines in one class.
- **The loss rules are pure** (`trading_core.risk.limits`), and one `LossLimitActor` in `node/` feeds them.
  - **What the actor does.** It seeds in a worker thread, folds position events, and evaluates on events and on its own 1 s timer. It adds and clears blocks, and asks the `KillSwitch` to halt.
  - **Why a timer, not quotes.** The timer (not a quote subscription) is what lets blocks clear at midnight or at the cooldown's end while flat. It reads the latest quote from the cache.
  - **Runner up:** put the checks inside each strategy. Every new strategy would then have to repeat them, and could forget.
- **Kill switch orders are told apart by tags** (`origin:kill_switch`, `halt:<halt_id>`).
  - **The mapper.** It becomes origin aware: it reads the tag into `orders.origin = kill_switch`, and allows a null `decision_id` for that origin only. That is what `OrderOrigin` already documents, and `MissingDecisionTagError` still guards `strategy` orders.
  - **The Trades page.** A new `ExitReason.kill_switch` names the exit there.
  - **Runner up:** record an exit decision with reason `kill_switch`. But a kill switch close is not a strategy decision, and the column for this already exists.
- **Events and the run.** `KillSwitch` and `LossLimitActor` take the same `record` callable the paper loop uses (with its `run_id`). In live, where there is no run yet, they log only.
- **The confirm step is a two step form rendered by the server.** The first POST without `confirm=1` returns the button area with "Confirm resume" and "Cancel". The second POST carries `confirm=1` and sends. It works without JavaScript, as spec 0004 requires of control buttons. Runner up: `hx-confirm`. That uses a browser dialog, needs JavaScript, and is easy to dismiss by reflex.
- **Settings live in a new `LossLimitSettings`** that the trader's settings subclass in every env. The `risk_` names put them in the strategy version config automatically. Runner up: add the new settings to `PaperLoopSettings`. Live needs them too, and that class is the paper loop's.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**:

There is no Postgres migration. This feature reads existing tables, writes existing kinds of rows, and adds one Redis key and some enum values.

| Store | Read or written | Fields used | Notes |
|---|---|---|---|
| Redis `{env}:trader:halt` (new) | written by the trader on halt, deleted on resume; read at start | `reason`, `source`, `halted_at`, `halt_id` (JSON) | No TTL, AOF. The trader's Redis user already has `~*`. The dashboard does not read it; it reads the snapshot. |
| Redis `{env}:trader:pause` | deleted on resume | unchanged (spec 0005) | |
| `positions` | read at start (seed) | `env`, `run_id`, `position_ref`, `status`, `realized_pnl`, `opened_at`, `closed_at` | Joined to `strategy_runs` on `env`, with the run mode matching the trader's (`paper` in demo), the same filter as the dashboard's `_realized` |
| `risk_events` | written (`halt`, `resume`, `limit_hit`, `order_rejected`); read at start | `kind`, `severity`, `event_key`, `details`, `occurred_at` | The kinds already exist. The seed reads today's `halt` rows whose `details.reason` is `daily_loss_limit`. |
| `orders` | written through the recording mapper | `origin` = `kill_switch` and `decision_id` = null for tagged orders | The value already exists in `OrderOrigin` |
| `{env}:snapshot:trader_state` | written | the AC-16 fields | Read by the dashboard |

New Python values (none stored in a DB CHECK):
- `ControlSource.CLI = "cli"`.
- `BlockReason.DAILY_LOSS_LIMIT`, `TRADE_CAP`, `LOSS_COOLDOWN`, `LOSS_UNKNOWN`, all added to `SELF_CLEARING` with plain words. `BlockReason.HALTED` is added too, shown like `PAUSED` (from the gate, not from `EntryBlocks`).
- `HaltReason` in `control/halt.py`: `manual_stop`, `daily_loss_limit`, `total_loss_limit`, plus `unknown` used only in memory and in the snapshot.
- `HaltState(reason, source, halted_at, halt_id)`.
- `check.HALTED = "halted"`.
- `ExitReason.KILL_SWITCH` for the Trades page.

Pure types in `trading_core.risk.limits` (a position key is `PositionKey = tuple[str, int]`, meaning `(position_ref, opened_ns)`):
- `LossLimits(daily_usd, total_usd, total_since: date, max_trades_per_day, cooldown_s)`
- `LossTally(day: date, realized_today, realized_total, trades_today, opened_keys: frozenset[PositionKey], closed_keys: frozenset[PositionKey], last_loss_closed_at: datetime | None, daily_tripped: bool, total_tripped: bool)`, frozen
- `LimitVerdict(trip: HaltReason | None, blocks: frozenset[BlockReason], loss_today, loss_total, cooldown_until: datetime | None)`
- `roll(tally, now) -> LossTally`, which starts a new day at UTC midnight: it zeroes today's figures and clears `daily_tripped`. It keeps the keys counted in the total.
- `on_opened(tally, key) -> LossTally` and `on_closed(tally, key, realized, closed_at) -> LossTally`, each a no op for a key already counted
- `evaluate(tally, unrealized, limits, now) -> LimitVerdict`. `trip` is set only on the edge: daily when not `daily_tripped`, total when not `total_tripped`, with total winning.
- `tripped(tally, reason) -> LossTally`, which the actor calls after `halt_automatic` to set the matching flag

**State transitions** (the trader's trading state; pause stays as spec 0005 defined it):

```
running ──stop / daily trip / total trip──▶ halting (flattening, halt key written, REDUCING)
halting ──flat──▶ halted (REDUCING)
halting ──20 s, not flat──▶ halting (failed flatten_timeout; exit retries continue)
halted ──resume (flat, total under limit)──▶ running (ACTIVE; resume clears any pause too)
halted ──resume (total at or over limit)──▶ halted (failed total_loss_limit)
halting ──resume──▶ halting (failed not_flat)
any ──flatten──▶ same state, positions closing (flattening = true until flat)
start ──halt key present or unreadable──▶ halted (then flatten 5 s after start)
```

The entry blocks run beside this, each on its own rule: `daily_loss_limit` clears at UTC midnight, `trade_cap` at UTC midnight, `loss_cooldown` when its time passes, and `loss_unknown` when the seed read succeeds.

**API surface**:

Commands on `{env}:control` (format unchanged, spec 0004; the trader answers on `{env}:control:results`):

| Command | Sent by | Effect | Outcomes |
|---|---|---|---|
| `stop` | dashboard (one tap), `control_cli stop` | AC-1, AC-3 | `executed` when flat · `failed`: `flatten_timeout`, `halt_key_write_failed`, `error` · `expired` · `duplicate` |
| `flatten` | dashboard (confirm), `control_cli flatten` | AC-2, AC-3 | `executed` when flat · `failed`: `flatten_timeout`, `error` · `expired` · `duplicate` |
| `resume` | dashboard (confirm), `control_cli resume` | AC-4 | `executed` · `failed`: `not_flat`, `total_loss_limit`, `halt_key_delete_failed`, `error` · `expired` · `duplicate` |
| `pause`, `unpause` | unchanged (spec 0005) | | |

Dashboard routes (all need the session and CSRF, like spec 0004's stop):

| Route | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/control/stop` | POST | `csrf` | stop area + command state | session | 503 Redis down, 403 CSRF (unchanged) |
| `/control/flatten` | POST | `csrf`, `confirm` (opt) | without `confirm`: the confirm area; with `confirm=1`: appends `flatten` | session | 503, 403 |
| `/control/resume` | POST | `csrf`, `confirm` (opt) | as flatten, appends `resume` | session | 503, 403 |
| `/control/unpause` | removed from the dashboard | | Resume replaces it; `control_cli unpause` stays for the deploy | | |

Command line (`docker compose exec dashboard python -m dashboard.control_cli`):

| Subcommand | Options | Exit codes |
|---|---|---|
| `stop`, `flatten`, `resume` | `--wait-result`, `--ttl` (default 60) | 0 when `executed`; 1 when `failed`, `expired`, or `duplicate` (reason printed); 2 when Redis cannot be reached or no result within the TTL |

Core functions:
- `check_entry(EntryRequest(..., halted: bool))`
- `target_trading_state(paused: bool, halted: bool) -> TradingStateName`
- `halt.encode`, `halt.decode`, `halt_key(env)`
- `limits.roll`, `on_opened`, `on_closed`, `evaluate`, `tripped`
- `recording.queries.loss_seed(session, env, mode, day_start, total_since) -> LossSeed`. It holds the realized sums, the newest losing `closed_at`, and whether today has a daily halt event. It also holds the `(position_ref, opened_ns)` keys it counted: opened today, and closed in the total window. The actor uses these to skip events already in the seed.

**Value sourcing**:

| Action | Value produced or displayed | Source |
|---|---|---|
| stop / auto halt | `halt_id` | the command's `id`; a new UUID7 for an automatic halt |
| stop / auto halt | `halted_at` | the trader's clock (`clock.utc_now()`) when the halt is applied |
| stop / auto halt | `source` | the command's `source` field; `trader` for an automatic halt |
| stop / auto halt | `reason` | `manual_stop` for a command; `LimitVerdict.trip` for an automatic halt |
| flatten | close order quantity and side | the NautilusTrader cache's open position (`position.quantity`, opposite of `position.side`), as in spec 0006 AC-8a |
| stop / flatten | the strategies to flatten | the `KfsStrategy` list `main.py` hands the `KillSwitch` after the strategies are created; empty in live |
| stop / flatten | "flat" for `executed` | cache: no open position, and no entry order (not reduce only) that is open, in flight, or pending cancel, over all strategies |
| stop / flatten | timeout | `KILL_SWITCH_FLAT_TIMEOUT_S`, counted by the trader's clock from when `KillSwitch` starts executing the command (or the automatic halt) |
| resume | `not_flat` | `KillSwitch.flattening` (a stop's flatten is running) or the cache showing an open position while halted |
| resume | `total_loss_limit` | `LimitVerdict.loss_total` from the latest evaluation, compared with `RISK_TOTAL_LOSS_LIMIT_USD` |
| resume | `loss_unknown` | `LossLimitActor.seeded` is false |
| position identity | key | `(position_ref, opened_ns)`: from the seed's `positions` columns; live from `event.position_id.value` and `event.ts_opened` |
| loss_today / loss_total | realized part | seed: `SUM(positions.realized_pnl)` with `closed_at` since UTC midnight / since `RISK_TOTAL_LOSS_SINCE`; then `PositionClosed.realized_pnl` per new key |
| loss_today / loss_total | unrealized part | `position.unrealized_pnl(mid)` for each open position in the cache, on each 1 s tick; mid from `cache.quote_tick(instrument)` (bid plus ask over 2). With no quote yet, it counts as 0, and the existing `stale_feed` block already stops entries. |
| UTC midnight | day boundary | the trader's clock in UTC (`date` of `clock.utc_now()`), checked on each 1 s tick |
| trades_today | count | seed: `COUNT(positions)` with `opened_at` since UTC midnight; then each `PositionOpened` with a new key |
| cooldown_until | time | the newest `closed_at` (seed) or `PositionClosed.ts_closed` (live) of a position with `realized_pnl` < 0, plus `RISK_LOSS_COOLDOWN_S`. A position closed as abandoned at a demo restart carries the restart time, so its cooldown starts then. |
| entry decisions | `halted` | `PauseGate.halted`, read by each strategy into `EntryRequest` |
| events | `record` and `run_id` | the paper loop's recording queue and run (demo); none in live, log only |
| daily_tripped after a restart | flag | seed: realized today at or over the limit, or a `risk_events` row with `kind = halt`, `details->>'reason' = 'daily_loss_limit'`, and `occurred_at` since UTC midnight in this env |
| all limits | thresholds | `LossLimitSettings` (below) |
| snapshot limits | `daily_loss_limit_usd` and the others | the trader's own settings, published in `trader_state`, so the dashboard holds no copy of them |
| dashboard HALTED badge | reason, age | `trader_state.halt_reason`, `halted_at` |
| dashboard command state | failure reason words | `{env}:control:results` `reason` field, mapped to plain words in the dashboard |
| CLI | `source` | always `cli` |

**Key invariants**:
- While halted, no order that is not reduce only reaches the exchange. `check_entry` and the gate in `KfsStrategy.submit_order` each refuse it. The risk engine in `REDUCING` adds a third layer only while a position is open: it refuses an order that would grow it, but not a new entry from flat.
- The risk engine's trading state is set in one place (`apply_trading_state`) from both flags, so no pause path can lift a halt's `REDUCING`.
- A protective stop is never cancelled while its position is open (spec 0006, unchanged). The flatten never cancels reduce only orders.
- The trader never reports `executed` for stop or flatten while any position is open.
- A halt is applied in memory before any I/O, so a Redis or Postgres failure can only leave the trader more halted, never less (fail closed).
- The halt key and the pause key are deleted only by a successful `resume` (and `unpause` for the pause, as before). No timer clears a halt.
- A position counts once in the loss figures and the trade count, whether it came from the seed or a live event.
- Loss figures are `Decimal` USD. No float touches them.
- `daily_loss_limit` and `trade_cap` never clear before the next UTC midnight.
- The total limit is never raised or reset by anything but config and a restart.

**Security model**: only an authenticated dashboard session with a valid CSRF token, or a shell on the VPS (the CLI), can send `stop`, `flatten`, or `resume`, the same as `pause` today (spec 0004). The dashboard still writes only to the `control` stream. It never reads or writes the halt key, and never writes Postgres. There is no new secret. Loss figures are not sensitive beyond what the dashboard already shows.

**Configuration required** (non secret, env vars validated in `trading_core.settings`, set for the trader in `compose.yaml`):
- `RISK_DAILY_LOSS_LIMIT_USD`: the daily loss that halts (default `20`)
- `RISK_TOTAL_LOSS_LIMIT_USD`: the total loss that halts and refuses resume (default `100`)
- `RISK_TOTAL_LOSS_SINCE`: the UTC date the total counts from. No default: `compose.yaml` sets it, and the trader refuses to start without it. For demo, use the date you first build this.
- `RISK_MAX_TRADES_PER_DAY`: positions opened per UTC day before `trade_cap` (default `50`)
- `RISK_LOSS_COOLDOWN_S`: seconds of `loss_cooldown` after a losing close, `0` turns it off (default `600`)
- `KILL_SWITCH_FLAT_TIMEOUT_S`: how long stop and flatten wait for flat before answering `failed` (default `20`)
- `SANDBOX_REJECT_KILL_SWITCH_ORDERS`: a demo only drill, like spec 0006's `SANDBOX_REJECT_STOP_ORDERS`. The sandbox rejects every order tagged `origin:kill_switch`, so the `flatten_timeout` and `not_flat` paths can be checked by hand. The default is `false`, and the trader refuses to start with it on outside demo.

**Critical test scenarios** (each maps to an acceptance criterion):
- **Happy path.** In demo with a position open, tap stop. Entry orders are cancelled, the position closes, the stop order is cancelled by `on_position_closed`, the result is `executed` within seconds, and the top bar shows `HALTED · manual stop`. Restart the trader: it is still halted. Resume: running. Verifies **AC-1**, **AC-3**, **AC-4**, **AC-5**, **AC-7**, **AC-17**.
- **Late fill.** An entry in flight when stop arrives fills after the halt. It gets its stop, then is closed within the same wait. Verifies **AC-3**.
- **Several round trips.** Three round trips in one day under NETTING (same position id) count as three trades and three realized results. Verifies **AC-8**, **AC-11**.
- **Deploy unpause on a halted trader.** The trading state stays `REDUCING`. Verifies **AC-20**.
- **Close refused.** With the sandbox refusing the close, stop answers `failed`/`flatten_timeout` after 20 s, a critical event is written, the exit retries continue, and resume answers `not_flat`. Verifies **AC-3**, **AC-4**.
- **Daily limit.** With `RISK_DAILY_LOSS_LIMIT_USD` set just under the open position's unrealized loss, the trader halts with `daily_loss_limit`. Resume lifts the halt, but `daily_loss_limit` stays in `entries_blocked`. A restart the same day keeps the block. After UTC midnight (a pure test with a fake clock) it clears. Verifies **AC-8**, **AC-9**, **AC-13**, **AC-14**.
- **Total limit.** It trips and resume answers `total_loss_limit`. After a raised limit and a restart, resume works. Verifies **AC-10**.
- **Trade cap and cooldown.** At the cap, entries are refused with `blocked:trade_cap`. After a losing close, entries are refused with `blocked:loss_cooldown` until close time plus the cooldown, also across a restart. Verifies **AC-11**, **AC-12**, **AC-15**.
- **Fail closed.** With Postgres down at start, the trader runs with `loss_unknown` blocking entries, and it clears when Postgres returns. A malformed halt key starts the trader halted. A Redis write failure on stop still halts and flattens, answering `halt_key_write_failed`. Verifies **AC-5**, **AC-6**, **AC-13**.
- **Seed and live overlap.** A position closed between subscribing and seeding is counted once. Verifies **AC-13**.
- **Auth and permission.** A POST to `/control/flatten` or `/control/resume` without a session or with a bad CSRF token is refused (303 to login, or 403) and appends nothing. Verifies **AC-18**.
- **Deploy.** A deploy on a halted trader leaves it halted. Verifies **AC-20**.
- **Bad config.** A total limit below the daily one refuses the trader's start. Verifies **AC-21**.

## Build plan

Tracer Bullet: the first slice is the thinnest real stop from the dashboard button to a flat, halted trader and a confirmed top bar. Each later slice thickens one segment end to end. There is no migration in any slice.

1. **Halt state in core.** (built)
   - `control/halt.py`: `HaltReason`, `HaltState`, `halt_key`, `encode`, `decode` (mirrors `control/pause.py`), with unit tests.
   - `target_trading_state(paused, halted)`.
   - `check_entry` gains `halted`, checked first, plus `HALTED` in the info refusals, with tests.
   - `BlockReason.HALTED` and `entries_blocked(blocks, paused, halted)`.

   Satisfies **AC-5**, **AC-15**.
2. **The thin stop.** (built)
   - **The gate.**
     - `PauseGate` gains `halt(state)`, `halt_in_memory()`, `unhalt()`, `halted`, and `halt_state`.
     - One `apply_trading_state()` sets the risk engine from both flags; `PauseController` switches to it.
   - **The strategies.**
     - `KfsStrategy.submit_order` and `submit_order_list` deny entries while halted (`entry_denied_halted`).
     - The base class gains a `flatten(halt_id)` hook.
     - `PlaceholderTimed` and `RandomTaker` pass `halted` into `EntryRequest`, and check it before submitting.
   - **`PlaceholderTimed.flatten`.**
     - Hold the kill switch tags as state until `_reset`, used by every `_submit_exit`.
     - With a position: run or adopt the exit loop.
     - Without one: cancel the entry order and set `kill_switch_pending`, so a later entry fill places its stop and then starts the exit loop.
     - Cancels while halted or flattening log at INFO with no `order_rejected` event.
     - `RandomTaker` gets a simple version, for the lab.
   - **The recording side.** The mapper becomes origin aware: it reads `origin:kill_switch` into `orders.origin` and allows a null `decision_id` for that origin only. Add `ExitReason.KILL_SWITCH` to the round trip query.
   - **`KillSwitch.stop`** in `node/control.py`:
     - It is given the `KfsStrategy` list from `main.py` and the `record` callable.
     - It halts in memory, applies the trading state, and then, at the same time, writes the key and flattens every strategy.
     - It writes the `halt` event.
     - It polls the cache every 250 ms until flat or the timeout (from when it started executing), calling `flatten` again for any position that appears.
   - **Wiring.** Wire `stop` in the controller's `execute`.
   - **At start.**
     - `ControlActor.on_start` reads the halt key with the pause key, and applies the state once.
     - When halted, it sets a one time alert 5 s later that calls `KillSwitch.flatten_after_restart()`.
   - Add the demo drill `SANDBOX_REJECT_KILL_SWITCH_ORDERS`, the same way `SANDBOX_REJECT_STOP_ORDERS` is built.
   - Reword spec 0004 verify step 21 and spec 0005 AC-6 to the new behavior.

   Satisfies **AC-1**, **AC-3**, **AC-5**, **AC-6**, **AC-7**.
3. **Flatten and resume, end to end.** (built)
   - `KillSwitch.flatten` and `KillSwitch.resume`, with the `not_flat` refusal, and `executed` when resume finds no halt (it still clears a pause).
   - The dashboard: `/control/flatten` and `/control/resume` with the two step confirm, Resume switched from `unpause` to `resume`, and the failure reason shown in the command state.
   - `ControlSource.CLI`, plus the `control_cli` subcommands `stop`, `flatten`, and `resume`.
   - A check that a deploy's pause and unpause leave the halt alone.

   Satisfies **AC-2**, **AC-4**, **AC-18**, **AC-19**, **AC-20**.
4. **Pure loss rules.** (built)
   - `risk/limits.py`: `LossLimits`, `LossTally`, `LimitVerdict`, `roll`, `on_opened`, `on_closed`, `evaluate`, `tripped`, keyed by `(position_ref, opened_ns)`. Unit tests cover each AC edge:
     - at the limit;
     - the midnight roll;
     - both limits at once;
     - a key counted twice;
     - three round trips with the same `position_ref`;
     - no second trip after `tripped`;
     - cooldown 0.
   - `LossLimitSettings` with its validators.

   Satisfies **AC-8**, **AC-9**, **AC-10**, **AC-11**, **AC-12**, **AC-21**.
5. **Loss limits live.** (built)
   - `recording.queries.loss_seed`, with an integration test on real Postgres.
   - **`LossLimitActor`** in `node/`, registered after the `ControlActor` and the demo's abandoned close.
     - It subscribes to position events first, holding them in a buffer.
     - It then seeds in `asyncio.to_thread`, retrying every 30 s with `loss_unknown` until the seed succeeds.
     - It folds the buffered events, skipping keys already in the seed.
     - It evaluates on each event and on a 1 s timer that runs flat or not. The timer reads the latest quote from the cache, rolls the day, and clears blocks whose time is up.
   - **What it does with the result.**
     - It adds and clears the four blocks.
     - On a trip, it calls `KillSwitch.halt_automatic(reason)` and then `tripped`. `halt_automatic` runs its flat wait as its own task, keyed by `halt_id`.
     - It writes the `trade_cap` `limit_hit` once per day.
   - **`KillSwitch.resume`** reads its `seeded` flag and `loss_total` for the `loss_unknown` and `total_loss_limit` refusals.

   Satisfies **AC-9**, **AC-10**, **AC-11**, **AC-12**, **AC-13**, **AC-14**, **AC-4**.
6. **Snapshot and dashboard view.**
   - `trader_state` gains the AC-16 fields, read from the gate, the `KillSwitch`, and the `LossLimitActor`.
   - The dashboard's top bar gets the `HALTED` badge.
   - The System page gets the loss meters, trades today, and cooldown.
   - `PLAIN_WORDS` gets entries for the new block reasons.
   - Update the styleguide samples.

   Satisfies **AC-16**, **AC-17**.
7. **Prove it in demo.** Run [verify.md](verify.md) on the laptop stack, then down. Satisfies every AC.

## Consequences

**Positive**:
- **The red button works.** It finally does what it looks like it does, and it leaves you flat, not just paused.
- **Losses stop by themselves.** A broken strategy or a bug can lose at most a known USD amount a day, and a known amount overall, before the bot stops itself.
- **Halts survive restarts.** A halt survives restarts, deploys, and reboots, so autoheal or a deploy cannot quietly undo it.
- **One place for every strategy.** The checks live in one actor and one pure function, so every later strategy gets them for free.

**Negative / tradeoffs**:
- **Fixed USD limits.** The limits are fixed USD amounts. As the account grows or shrinks, you must change them by hand, or they become too loose or too tight.
- **Unrealized loss trips the limit.** A brief price spike can halt you on a position that would have recovered. That is the price of stopping a real loser before it closes.
- **No same day override.** The daily block cannot be overridden the same day, by design, even when you know the loss was a fluke.
- **The total limit needs a keyboard.** It needs a config change and a deploy to clear. That's slow on purpose, and painful when you are away from a laptop.
- **A long wait on the queue.** A stop that cannot get flat holds the control consumer for up to 20 s. A pause or second stop sent meanwhile waits behind it.
- **A small gap on key write failure.** If Redis rejects the halt key write and the trader restarts before you notice, it comes back running. The dashboard's "Stop failed" bar is the only warning until feature 12's phone alerts.
- **Only known positions.** Flatten closes only positions NautilusTrader knows about. A position opened by hand on Kraken, or one the cache lost, is not closed (feature 11).
- **A shutdown mid flatten.** A SIGTERM or crash in the middle of a flatten cancels the wait and the exit timers. The restart flatten (AC-7) picks the position up 5 s after start. The pending stream entry is then read again and answered `duplicate`, so the CLI exits 1 for a stop that did run.
- **A demo restart can realize a loss.** It closes open positions as abandoned at the last mark, which can realize a loss that counts toward today's limit and starts a cooldown from the restart time.
- **Profits offset losses.** The daily figure nets open profit against realized loss, so a winning open position can hide an earlier loser until it closes. That is the equity view you chose, not a hard floor on realized loss alone.

**Neutral**:
- **Resume clears more than before.** The System page's Resume now clears a pause too, whoever set it. That is what spec 0005 AC-13 already said Resume should do.
- **The strategy version changes.** The `risk_` settings join the strategy version config, so the first deploy after this feature records a new strategy version.
- **New risk event rows.** `halt`, `resume`, and the `trade_cap` `limit_hit` rows start appearing in `risk_events`. Feature 12 will alert on the critical ones.

## Follow-up

- [ ] **Feature 11.** Close positions and orders on Kraken that NautilusTrader does not know about during a live flatten, and consider Kraken Futures' "cancel all orders after" dead man's switch (orders cancelled by the exchange if the bot goes silent).
- [ ] **Feature 12.** Alert on every `critical` `halt` event, on `flatten_timeout`, and on `halt_key_write_failed`.
- [ ] **First live strategy.** Wire a live recording writer and run for `KillSwitch` and `LossLimitActor` events, which live only logs until then (AC-7). `EntryBlocks` is created in the paper loop today; give it an owner that exists in live too.
- [ ] **Feature 10.** Once funding reaches `positions.realized_pnl` (or a sum next to it), count it in the loss figures (AC-8).
- [ ] **Feature 15.** The trade cap and cooldown defaults are guesses; tune them from lab and paper results.
- [ ] **Spec 0004.** Its verify step 21 and the "feature 9 adds" notes, and spec 0005's AC-6 and its follow-up line, describe the old `not_implemented` answer; reword them in build task 2. `/sync` should confirm.
- [ ] `htmx` and `fastapi` are installed skills not yet listed in root `AGENTS.md` `## Agent skills`. They belong in [apps/dashboard/AGENTS.md](../../../apps/dashboard/AGENTS.md), since only the dashboard uses them.
