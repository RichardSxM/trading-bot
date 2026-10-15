# 0010. Rationale: kill switch and loss limits

The decision record behind [index.md](index.md). `/develop` builds from index.md and can skip this file.

## Context

The bot trades BTC perpetuals by itself, around the clock, on a VPS you reach only over Tailscale. Specs 0004 and 0005 built the whole command path:
- a red stop button on every dashboard page;
- a `{env}:control` stream with results and Postgres records;
- a `ControlActor` in the trader that pauses and unpauses.

But `stop`, `resume`, and `flatten` are still answered `failed`/`not_implemented`. Today the only way to get flat in a hurry is to log into Kraken by hand. That is slow on a phone, and it is exactly the moment you are least calm. Slice 4 of the scope makes this guard, plus leverage caps (feature 10) and restart safety (feature 11), a hard requirement before any live money.

Four forces shape the problem.
- **Flatten safely.** The flatten must never leave a position unprotected. Spec 0006 keeps a reduce only stop order on every position and never cancels it while the position is open. A kill switch that cancels "everything" first would break that.
- **Reach the exchange.** NautilusTrader's risk engine has its own trading states (`ACTIVE`, `REDUCING`, `HALTED`), and the obvious one, `HALTED`, refuses the very reduce only close the flatten needs.
- **Count losses across restarts.** In demo the simulated account resets to 1000 USD on every restart, so "how much did I lose today" cannot come from the account balance. It has to come from the recorded positions in Postgres.
- **Fail toward halted.** The dashboard may not write Postgres, and every component that touches orders must fail closed. When Redis or Postgres misbehaves, the trader has to end up more stopped, not less.

There is also a behavioral force. A scalper (a strategy that takes many small, short trades) pays fees on every round trip and can bleed slowly by overtrading. And a human who just took a loss is tempted to override the guard ("revenge trading"). The articles noted in the scope suggest a daily loss limit of 2 to 3 % of the account, a cap on trades per day, a short cooldown after a loss, and a losing streak stop. You chose the first three.

Without this feature, a strategy bug or a bad market can lose without bound until you notice, and the red button on the dashboard is a lie.

## Options considered

### Option 1: Stop as pause plus Kraken by hand

Make `stop` an alias of `pause` (block entries only), and leave flattening to you on the Kraken site. Add the loss limits as more `entries_blocked` reasons, checked inside each strategy's entry path.

**Pros**:
- Smallest change. Pause already works end to end, and there's no new state or key.
- No order is ever sent by the kill switch itself, so it cannot misfire into a wrong order.

**Cons**:
- **Not flat.** The scope's "flatten everything within seconds" is not met. A losing position stays open while you find your laptop.
- **Limits per strategy.** The loss limits live in each strategy, so every new strategy must remember to call them.
- **Resume undoes too much.** A pause is lifted by any Resume, including the deploy's own unpause path, so it is too easy to undo.

### Option 2: Trader side kill switch over the control stream, pure loss rules in core (chosen)

The trader's `ControlActor` runs a `KillSwitch` that halts, cancels entry orders, and asks each strategy to close its position through its own exit loop. It answers only once flat. The halt is persisted in its own Redis key, and the risk engine sits in `REDUCING`. A `LossLimitActor` seeds the loss figures from Postgres and keeps them current from position events. It feeds a pure `evaluate` function in core, and calls the `KillSwitch` on a trip.

**Pros**:
- **Reuses tested pieces.** It reuses the command stream, the result format, the dashboard command state, and the strategy exit loop already tested in spec 0006. The only new store is one Redis key.
- **Stays protected while closing.** The protective stop stays on until flat, because the close goes through the same reduce only path as a normal exit.
- **Survives restarts.** The loss figures survive restarts (Postgres seed) and are fresh between them (events), without waiting for the recording writer.
- **One place for every strategy.** The rules are one pure function, unit testable with a fake clock, and shared by every strategy.

**Cons**:
- **Most code.** It adds two runtime components (the `KillSwitch` and the `LossLimitActor`) and a second key the trader must read at start.
- **Two sources to merge.** Seed plus events is two sources to reconcile, which needs care to count each position exactly once.
- **The consumer waits.** Waiting for flat holds the control consumer for up to 20 s.

### Option 3: NautilusTrader's own tools: `HALTED` and `close_all_positions`

Use the risk engine's `HALTED` state as the halt, and NautilusTrader's built in `cancel_all_orders` and `close_all_positions` on each strategy for the flatten. Keep the loss check in a small actor that flips the state.

**Pros**:
- Uses framework features as designed, with less of our own code.
- `close_all_positions` handles any number of positions and instruments.

**Cons**:
- **The halt blocks the close.** `HALTED` denies new orders, the reduce only close included. The flatten would have to run before the halt, which leaves a window where new entries can slip in.
- **No retries.** `close_all_positions` has no retry loop and no `exit_failed` handling. A rejected close is simply gone, which is what spec 0006's exit loop exists to prevent.
- **The protective stop goes too.** `cancel_all_orders` also cancels the protective stop before the position is closed.

### Option 4: An external watchdog process

A separate small service watches the snapshots and Postgres, and on a stop or a limit it calls Kraken directly to cancel and close, independent of the trader.

**Pros**:
- **Works when the trader is stuck.** It still works if the trader process is hung or crashed.
- **Independent.** Separate code is a second, independent line of defense.

**Cons**:
- **Live keys in a second process.** It needs the Kraken trading key, which spec 0003 restricts to the trader. That is a second process holding live keys.
- **Two order senders.** Two processes would send orders for the same account, with no shared view of the orders in flight. That is a classic source of duplicate or crossing orders.
- **No demo.** It doesn't work in demo at all, since the demo exchange lives inside the trader's process.
- **Wrong feature.** It's really feature 11 and 12 territory (outage safety and health checks), not this one.

## Rationale

Option 2 is the only one that meets the scope's done criterion, "flatten everything in demo within seconds, and stay halted until you resume", without breaking a rule an earlier spec relies on.

**Option 1 and Option 3.** Option 1 does not flatten at all. Option 3 flattens, but through framework calls that cancel the protective stop and drop a rejected close, which undoes spec 0006's two hardest won guarantees. Its `HALTED` state also blocks the close.

**Option 4.** Option 4 solves a real problem, a hung trader. But it does so by putting live keys in a second process and creating two independent order senders, which is a worse failure mode than the one it fixes. It belongs with the outage work in features 11 and 12, where Kraken's exchange side "cancel all orders after" switch is the safer form of the same idea.

**Mapping a halt to `REDUCING`.** It looks odd to call a halt `REDUCING`, but it is the state whose meaning matches: nothing may grow a position, everything may shrink it. Our gate already tells a pause from a halt. The extra `HALTED` state would only add a transition to get wrong.

**Fixed USD limits.** Keeping the limits in fixed USD follows from the demo account resetting on every restart. A percentage needs a trustworthy equity base, and demo has none. A fixed amount is also easier to reason about at 2am.

**The total limit needs config.** Making the total limit need a config change, rather than a tap, is deliberate. It is the line you set while calm, and the phone is where you are least calm.

**The daily block outlives Resume.** Letting Resume lift the daily halt while the entry block holds until midnight gives you control of the halt (you can let protective logic run, watch, and decide), without giving you a way to trade more today.

**Seed plus events.** Seeding from Postgres and then folding NautilusTrader events is the one way to be both restart proof and fresh. Polling Postgres alone would lag the recording writer, and would go blind exactly when the database is in trouble. Memory alone forgets the day on every restart.

**Failing closed.** When the seed fails, entries are blocked (`loss_unknown`), because a limit you cannot measure is a limit you cannot honor.

**The cost.** The price is the most code of the four options, and a control consumer that can be busy for up to 20 s. Both are acceptable for a component whose whole job is to be the last line of defense.

### Proving the two race only points (2026-10-12)

The first `/check verify` runs proved every step except two points that depend on timing.

**AC-1's entry cancel gets a drill.** A sandbox market entry is in flight for about 75 ms, at 1.97 to 2.75 s after the boundary, and a hand timed stop at 14:35:02.05 UTC missed it. Retrying the race would be luck, not evidence. Accepting the unit test alone (`test_node_placeholder.py`, `entry_canceled_kill_switch`) was the runner up, but cancelling an entry in flight is the core promise of the stop button, so it deserves a runtime check. `SANDBOX_DELAY_ENTRY_FILLS_S` holds the entry long enough to tap stop, and it mirrors how a real venue behaves: an order acknowledged but not yet worked, then cancelled. It is built like the existing drills (demo only, off by default, refused elsewhere), so it adds no new kind of risk. NautilusTrader's own latency model was the other way to slow fills, but it delays every order, stops and exits too, and a cancel racing a queued submit does not give a clean SUBMITTED to CANCELED. A hold on entries alone tests exactly the AC-1 path and nothing else.

**AC-15's base class deny rests on its unit test.** With today's strategies, the base deny cannot be reached at runtime: each strategy checks `halted` right before it submits, in the same call on the one event loop thread, so no halt can land in between. A drill to force it would mean a strategy that skips its own check, test code in the trader. The engine level unit test already drives a real `BacktestEngine` through the real gate, which is the honest proof. The deny stays as the backstop for later strategies.
