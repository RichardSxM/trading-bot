# 0009. Rationale: market context feed

## Context

Scope feature 20 asks for the perp market's own context next to prices: funding rate, open interest, mark versus index basis, and liquidations, recorded continuously with gaps logged, readable by a backtest at the moment each value was known, and readable live by a strategy. Much of this history cannot be bought later at the resolution we record it, so every week without recording is a week lost for testing regime filters.

The work lands in the recorder workspace (`apps/recorder`) with pure rules in `packages/core`, plus small changes in `apps/trader` and `apps/lab`. The recorder (spec 0007) is a NautilusTrader node with data clients only. It writes feather files per hour through the streaming writer, and a separate compactor turns closed hours into the Parquet catalog with gaps, manifests, a reorder window, and byte identical reruns. It records BTC only. Spec 0007 left a hook for this feature; spec 0008 left own replay days "before funding" until this feature lands.

Forces at play:
- **What the adapter gives.** NautilusTrader 1.231's Kraken adapter (checked in the recorder image) subscribes to mark price, index price, and funding rate on Kraken's futures websocket, with `MarkPriceUpdate`, `IndexPriceUpdate`, `FundingRateUpdate` types. It offers no open interest and no liquidation flag, although Kraken's `ticker` feed carries `openInterest` and its `trade` feed carries each trade's `type`.
- **Look ahead.** A backtest that reads a value before it was known looks better than live. The catalog already keys known time on `ts_init` (spec 0008 AC-1).
- **The book is irreplaceable.** Anything new in the recorder must not stop it, make it unhealthy, or cut book recording.
- **Project rules.** Pure logic in core, no floats for money, typed errors, Nautilus at the edges, everything in Docker on the London VPS, one trader working alone.

Engineer's choices (from the design conversation): BTC only; no extras (Fear & Greed, news blackout, Deribit deferred); no dashboard card; inside the existing recorder; every update stored; open interest and liquidations through one raw public websocket client using `websockets`; liquidations from non fill trade types; two Nautilus custom data classes; mark, index, open interest also imported from Tardis; basis derived on read; reconnect and log, never unhealthy; values carry age and a stale flag; liquidation rolling sums per side; own funding all or nothing per run; trader subscribes too, proven by a log line.

## Options considered

### Option 1: Native types only

Record mark, index, and funding through the adapter and nothing else. Open interest and liquidations wait for a future Nautilus release.

**Pros**:
- Smallest change: three subscriptions, three compactor tables, no new connection, no new dependency.
- Nothing of our own to maintain at the protocol level.

**Cons**:
- Misses two of the four inputs the feature exists for, and the history of both is the hardest to get later.
- No timeline: nothing says a Nautilus release will add them.

### Option 2: Native types plus one raw public websocket actor, in the recorder (chosen)

Native subscriptions for mark, index, and funding. A thin `ContextFeedActor` with the `websockets` library reads Kraken's `ticker` and `trade` feeds and publishes two custom data types on the bus, so the same streaming writer and compactor record them. One pure `ContextBook` reads everything.

**Pros**:
- Covers all four inputs with one extra connection and no keys.
- Reuses the whole spec 0007 pipeline (rotation, reorder, gaps, retention, backup) and the spec 0008 replay path.
- The same actor and reader run in the trader, so live and replay see the same values.

**Cons**:
- Our own websocket code, reconnect, and parser to maintain against Kraken's message format.
- Custom data types must stream through the feather writer; if they cannot, a side file fallback adds a converter.

### Option 3: Poll Kraken's public REST tickers

A timer calls `/derivatives/api/v3/tickers` every 10 s for open interest, funding, mark, and index; liquidations are left out or polled from a trade history endpoint.

**Pros**:
- Stateless and simple; no websocket lifecycle.
- One request covers several fields.

**Cons**:
- Coarse: 10 s samples miss the moves around a liquidation cascade.
- Liquidations would need a second path, and polled history can miss rows between polls.
- Duplicates what the native subscriptions already stream.

### Option 4: A separate context recorder service

Its own Nautilus node and Compose service, apart from the order book recorder.

**Pros**:
- Strongest isolation: a context bug can never touch the book recorder process.

**Cons**:
- A second node, health check, deploy target, and compactor wiring to operate for a few rows per second.
- The isolation it buys is already achieved inside one process by never letting context failures leave the actor (AC-4, AC-6).

## Rationale

The feature's value is in the inputs that cannot be bought back later, and open interest and liquidations are two of them. That rules out Option 1. Between the remaining ways to fetch them, a websocket gives every update at the moment Kraken sends it, which is what a cascade filter needs and what keeps `ts_init` honest, while REST polling (Option 3) samples coarsely and cannot see liquidations at all. The raw client stays small because it only parses two feeds for one product, and it is built to fail quietly: a broken context feed logs, records an outage, and reconnects, but never touches the book. That is why Option 2 lives inside the existing recorder rather than in a new service (Option 4): the risk Option 4 isolates is handled at the actor boundary, without a second node to operate.

The `websockets` library was chosen over Nautilus's internal Rust websocket client because that client is an undocumented pyo3 internal with no stubs: it would need type ignores and could change at any Nautilus bump, against the project's pin discipline. `aiohttp` would pull a whole HTTP framework in for one socket.

Two custom Nautilus data classes, rather than a side file, keep one write path, one rotation, and one compactor model, and let the backtest engine replay them to strategies through the same handlers as live. The gate proves this before anything is built on it, with the side file as a named fallback. One pure `ContextBook` keyed on `ts_init` is the core of the look ahead guarantee: replay and live run the very same fold, and a test compares a replay's view at time `t` with a fold of only the rows known by `t`. Basis is derived on read because mark and index arrive at different moments, and storing one pairing would bake in a rule that the reader can apply more honestly. Own funding is all or nothing per run so a result never mixes two cost bases.

## Amendment 2026-10-16: holes at a run's tail and a starved event loop

### Context

On the first VPS day (verify.md step 5), the raw context socket went silent from 2026-10-15 13:58:56 UTC until the recorder restarted at 15:05:36, and the gap file had no row for it. `/debug` found two causes.

First, the event loop was starved. Nautilus 1.231.0's `LiveDataEngine._run_data_queue` reads its queue with `await queue.get()`, which returns at once, without letting any other task run, while the queue holds items. Kraken's adapter fills that queue from its own thread, and the old per message writer (spec 0012's cause) kept it full at busy hours. So the feed's 30 s idle timer and its reconnect timeouts could not fire. The evidence: the open interest lag (`ts_init − ts_event`) grew from 0.2 s to 105 s before each outage that day, and a small asyncio repro with a full queue fired a 1 s timer zero times in 6 s. Since spec 0012 deployed (4d55abd), the longest open interest gap has been 3.8 s, but the way the loop is shared has not changed.

Second, the gap rules as written could not see it. The feed wrote an outage line only on recovery, and `silence` was found only between two rows of one run. An outage still open when the run ended left no trace. Worse, with the loop starved, even `context_feed_down` may never have fired.

### Options considered

**For the tail hole:**
- **An open outage line only.** Write the line at `down`, close it at the run's end. Pro: gives the raw streams their `reconnect` gap. Con: a starved feed may never reach `down`, which is the 2026-10-15 case.
- **A tail silence only.** The compactor alone, from a stream's last row to the run's end. Pro: needs nothing from the feed, so it catches a starved feed. Con: liquidations get no `silence` gaps (quiet spells are normal), so a dead liquidation feed at the tail goes unrecorded.
- **Both (chosen).** Each covers the other's blind spot, and both end at the run's end, so they never fight with `recorder_down`. A cross check asked whether the tail silence alone would do, since a starved feed never writes the open line anyway. It would not: a normal drop still open at a restart (Kraken maintenance followed by an autoheal restart, as on 2026-10-15 at 07:00) leaves liquidations with no other record.

**For the starvation:**
- **A lag warning (chosen).** Cheap, pure, in both nodes, and the lag grew for about 10 minutes before each outage, so it is an early sign. Con: it only tells you, and it stops nothing. It is measured as each message is handled, so a loop that stalls completely logs nothing at all. That hole is what the compactor's tail silence records, from outside the node.
- **Write it down only.** Rely on spec 0012's per minute queue stats. Con: nothing in the context feed itself would show it, and the trader has no such stats.
- **Run the feed on its own thread and loop.** The idle timer would fire, but publishing has to go back to the starved main loop (the message bus is not thread safe), so rows would pile up anyway. Con: more moving parts, no real gain.
- **Patch Nautilus's queue reader to yield.** Fixes the root cause. Con: against the exact pin discipline, and every Nautilus bump would carry the patch. Reported upstream instead (Follow-up).

### Rationale

The rule that matters is that every hole is on record (spec 0007's whole gap model rests on it), so both tail rules go in: the one that cannot be starved (the compactor's tail silence) and the one that covers liquidations (the open line). The run's end is taken from the book's `recorder_down` start because that is the one timestamp both the book and the context agree marks the end, and touching (not overlapping) it keeps both gaps under the existing overlap rule. The root cause of the starvation is in Nautilus and is already relieved by spec 0012, so this spec only makes it visible: a 10 s lag limit is far above today's worst spike (3.7 s) and far below the 105 s seen before the hole.
