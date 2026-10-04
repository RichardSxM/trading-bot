# 0006. Rationale: core paper trading loop

## Context

Feature 6 is the walking skeleton from the scope: live BTC perpetual prices, one deliberately simple strategy, a risk check with fixed caps, an entry with its stop, fills recorded, and the position and P&L on the dashboard. Nothing in the repo trades yet. The trader runs a node with a public Kraken data client, the control actor, and the `KfsStrategy` pause gate (spec 0005), but no strategy and no execution client in demo. Spec 0002's tables and idempotent repository exist, but nothing writes orders or fills. The dashboard shows heartbeat, pause, and commands, but no position or P&L.

The forces:
- **No exchange to practice on.** Kraken shut its futures demo on 14 July 2026. Demo now means real public prices plus NautilusTrader's sandbox execution client, which simulates fills inside the trader. The scope's "exchange side stop" can only be a stop resting in that simulation until feature 19. Spec 0001 made this feature prove the sandbox works with Kraken's `PF_` instruments, or come back here.
- **The point is the thread, not profit.** Feature 7's 72 hour soak, its deploy during an open position, and the restart drills all need something that trades on a predictable rhythm. Strategy research comes later (features 13 to 17).
- **One process holds positions.** The trader's event loop also runs the stop logic, so anything slow on it (a Postgres write, a Redis call) delays protection.
- **Fail closed on orders, never on exits.** The root rules require refusing any order action that cannot be checked or recorded. Exits and stops must still go through, or a protection failure turns into a loss.
- **The sandbox forgets.** Its simulated account lives in memory. A restart cannot restore a position, and the Nautilus cache in Redis would then disagree with it.
- **Clean Architecture.** Risk and sizing belong in `packages/core` as pure logic. They are reused by the backtest (feature 13) and live (feature 19).

## Options considered

### Option 1: thin timed strategy, pure risk check, sandbox, queued recording actor, Redis snapshots (chosen)

A `KfsStrategy` subclass trades on a fixed 15 minute beat with a market entry, a stop placed on fill, and a 10 minute time exit. A pure `check_entry` in core guards every entry. A recording actor maps Nautilus events to spec 0002 rows and hands them to a writer thread. The trader publishes position and account snapshots for the dashboard.

**Pros**:
- Every segment of the thread is exercised every 15 minutes, which makes the soak and drills easy to schedule.
- Pure core logic is unit tested without Nautilus and reused by later features.
- A slow or absent Postgres never delays a stop or an exit.

**Cons**:
- More moving parts than writing inline: a queue, a thread, spill files, and replay.
- The timed beat produces a steady fee loss with no signal behind it.

### Option 2: a Nautilus bracket order (entry, stop, take profit as one list)

**Pros**:
- Atomic in the sandbox: the stop exists the moment the entry fills, so there's no 5 s window to watch.

**Cons**:
- Kraken Futures has no native linked bracket, so live would emulate the link inside the trader, and the stop would not rest on the exchange until the entry filled. The demo code path would then differ from the live one that feature 19 needs.
- It adds a take profit leg and an OCO the placeholder does not need.

### Option 3: an EMA crossover on 1 minute bars

**Pros**:
- Looks like a real strategy and exercises indicator code.

**Cons**:
- Trade timing depends on the market. It can go hours without a round trip, which makes the soak's fill checks and the deploy during a position slow and unpredictable.

### Option 4: write to Postgres inline from the strategy

**Pros**:
- The fewest parts: no queue, no thread, no spill file.

**Cons**:
- A slow Postgres blocks the event loop that also runs the stop and time exit, which is exactly the failure the root rules forbid.

## Rationale

The deciding forces are "the thread is the point" and "never delay an exit". The timed beat (your pick) gives the soak and every drill a schedule they can rely on. The side rule (sign of the 5 minute return) still gives each decision real inputs to record, which keeps spec 0002's traceability meaningful. The stop goes on right after the fill (your pick) because that is the only pattern that carries to live Kraken unchanged. The 5 s acceptance timer with a flatten closes the gap a bracket would have hidden. The queued writer (your pick) costs a thread and a spill file, but it makes a database outage cost entries rather than exits, which is the fail closed rule applied correctly.

Sizes and caps mirror the live plan (your picks): 1,000 USD balance, 100 USD per trade, 2x leverage, 250 USD per order. The checks that ship now are then the ones feature 19 relies on. With 100 USD trades, the 2x cap never triggers unless something is broken, and that is what it is for. The 250 USD order cap sits 2.5 times above a normal trade, so a sizing bug (bad rounding, a zero price, a unit mixup) is refused while normal trades pass. It is set twice, in our check and in NautilusTrader's RiskEngine, so one bug cannot remove both.

The restart rule (your pick) accepts that the sandbox forgets. Restoring a simulated position is feature 11 territory, and refusing to start would break spec 0005's self healing. So a demo start deletes its own Nautilus cache keys, closes the leftover as abandoned at its last snapshot mark, and says so loudly. Deleting is allowed in demo only, because live must reload and reconcile with Kraken. Funding is deferred (your pick) because 10 minute positions make it small, and labeling the P&L "before funding" keeps that honest.

Calls I made inside the design (each with its runner up):
- **Bars from quotes inside Nautilus** (`1-MINUTE-MID-INTERNAL`) for the side, rather than keeping a quote history in the strategy. Nautilus aggregates them and they are the same in a backtest. Runner up: a deque of mids, more code for no gain.
- **Sandbox fills from quotes**, not the L2 book. A 100 USD order fills at the touch either way, and quotes need one subscription. Runner up: an L2 book subscription, which feature 14 needs for queue modeling anyway.
- **Boundaries aligned to UTC quarter hours.** Runs are comparable across restarts, and a drill can predict the next entry. Runner up: 15 minutes after start, which shifts with every restart.
- **The stop is placed once, when the entry is completely filled; a partial entry still open after 5 s is closed.** The sandbox fills 100 USD in one go, so resizing would guard a case that does not happen here. Runner up: resize with `modify_order` or cancel and replace, deferred to feature 19 if Kraken needs it.
- **A queue bound of 10,000 items, treated as a failure when full** rather than dropping. A dropped fill breaks the soak's count check, and 10,000 is hours of normal traffic.
- **`stop_not_accepted`, `exit_failed`, `record_conflict`, and `recording_failed` clear only on restart.** Each means a bug, not weather, and a restart is a deliberate act that resets the trader's view. Runner up: clearing on dashboard Resume, rejected because it would mix the deploy pause with fault handling.
- **Decide 2 s after the boundary**, using bars that end at or before it. The bar ending at the boundary has then closed, so a decision never depends on whether the aggregator was a few milliseconds late.
- **Settings for every number** (env vars through `trading_core.settings`), rather than a strategy config file. That is the project's one config path, and each change lands in `strategy_versions` through the config hash.

The cross check on the first draft (another model, on 2026-10-04) found gaps that you chose to fix as recommended, and they shaped the final design:
- **The time exit must never cancel the stop first.** That would leave a window with no stop, and spec 0005's soak would see it. So the close goes first, the stop is cancelled only after `PositionClosed`, and every exit runs through one loop that retries every second until flat, instead of giving up after a single retry.
- **Stop and exit races are normal, not faults.** A refusal that arrives once the position is flat is ignored, so a stop that fills during a time exit raises no alarm.
- **No `flush_on_start`.** NautilusTrader's flush could reach keys outside the trader's prefix (the pause key, the control stream), and the trader's Redis user is denied flush commands anyway. The trader deletes only its own prefix, by pattern, before the node starts.
- **The abandoned close covers every older run, whatever its status.** A clean SIGTERM while positioned ends its run, so "the previous running run" alone would have missed it. Such a run now ends `aborted` too.
- **Recording errors are split** into transient (retry in order), conflict, and permanent (dead letter, block until restart), so one bad item can never wedge the queue.
- **Dashboard sums count `paper` runs only**, because backtests share the demo env on the laptop. A missing snapshot shows "offline", never "flat".
- **AC-21 counts our own `fill_seen` lines** rather than NautilusTrader's log, which may print one fill on several lines.
