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
