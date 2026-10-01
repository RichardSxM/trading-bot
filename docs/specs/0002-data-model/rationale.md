# 0002. Rationale: data model

Decision record for [index.md](index.md). `/develop` skips this file.

## Context

Spec 0001 splits the bot into three processes. The trader writes every order, fill, position, and account event to Postgres through a recording actor. The dashboard reads Postgres and Redis and must never change trading records. The recorder writes market data to the Parquet catalog and never touches Postgres. Backtests and L2 replays (order book depth replays) run the same strategy code and produce the same kinds of records.

The forces that shaped this design:

- **Traceability.** The promotion gate (feature 15) and going live (feature 19) only mean something if "what passed" names an exact piece of code and configuration, and if every live order can be traced back to the decision that caused it.
- **At least once delivery.** The recording actor spills to a local file when Postgres is down and replays it later (spec 0001). Restart reconciliation (feature 11) may report orders the trader never recorded. The same record will arrive more than once, sometimes out of order, and sometimes before its parent.
- **Demo and live on one box.** Both environments can share one Postgres. A paper row mixed into live P&L, or the reverse, is a silent and expensive bug.
- **Exact money, kept for years.** Fills, fees, and funding feed P&L and a Dutch tax export, and Dutch retention rules want 7 years. Floats are banned by `AGENTS.md`.
- **One schema across modes.** Paper, live, backtest, and replay results must be comparable with plain SQL, or the gate becomes a pile of special cases.
- **Volume.** A seconds scale scalper produces snapshots and decisions far faster than an hour level strategy, and big replays produce many simulated fills. One person operates all of it on a 4 GB VPS.

Without this decision, feature 6 would invent tables under time pressure, and every later feature would inherit them.

## Options considered

### Option 1: Minimal ledger

Only `orders`, `fills`, and `funding_payments` in Postgres. Runs, decisions, and risk events stay in logs and Redis.

**Pros**: smallest schema, fastest to build, nothing to keep consistent.
**Cons**: no trace from an order to its decision or code version; the gate has nowhere to compare runs; risk history disappears when logs rotate.

### Option 2: Normalized schema, current rows plus events, idempotent repository (chosen)

18 tables: global reference tables (instruments, strategies, versions, promotions, FX rates) and env scoped tables tied to a run, with mutable current state rows (`orders`, `positions`) next to append only history (`order_events`, `fills`, snapshots). Writes go through a typed repository that relies on natural unique keys.

**Pros**: full traceability; one schema for all modes; the database enforces idempotency, env isolation, and the dashboard's read only role.
**Cons**: largest upfront surface; current rows can drift from history if a mapper is wrong; sim fills grow the database.

### Option 3: Option 2, with sim detail in Parquet

Same live and paper tables, but backtest and replay fills go to a Parquet file per run, and Postgres keeps only run summaries.

**Pros**: Postgres stays small no matter how many replays run.
**Cons**: two write paths and two schemas for fills; comparing sim trades with paper trades needs DuckDB plus Postgres; the gate's drill down gets harder.

### Option 4: Pure event sourcing

One append only `events` table holds everything. Orders, positions, and P&L are projections rebuilt from it.

**Pros**: perfect history, and no current state to drift.
**Cons**: every dashboard read aggregates or needs projection tables anyway; replaying projections after a schema change is heavy work for one operator; Nautilus already does the event sourcing in its own cache.

## Rationale

Option 2 is the only one that serves traceability, one schema across modes, and at least once delivery together. Option 1 fails traceability outright. Option 4 rebuilds what Nautilus already does, and the dashboard would pay for it on every read. Option 3 optimizes for a volume problem nobody has measured yet. Option 2 keeps that door open: if sim tables grow past a few GB, sim fills can move to Parquet without changing the live schema (Follow-up).

You picked these in the interview: market data in Parquet only (spec 0001 already settled it), an `env` column (paper and live in one query for the gate), a decisions table (rejections recorded too), current rows plus events, positions plus snapshots, versioned strategies, four run modes, funding from Kraken in live and computed in sim, risk events plus a control mirror plus account snapshots, promotions now, keep forever with thinned snapshots, UUIDv7, unscaled numeric, an instruments table, text plus CHECK enums, snapshots throttled to 1/s plus 60 s, actionable decisions only, ECB rates converted on export, shared mappers with a bulk write at run end, per process roles, downsampling run by backup, one migration, stub orders for orphan fills, and raising on a differing duplicate.

My own calls, each with its runner up:

- **Order `origin` column** with a CHECK that strategy orders need a decision. Kill switch, reconciliation, and unknown orders have no decision, so a blanket NOT NULL would force fake decisions. Runner up: a nullable `decision_id` with no check, which loses the guarantee.
- **`last_event_ns` guard** on `orders` and `positions`. Out of order delivery is expected (spill replay), and comparing timestamps is the cheapest way to never regress a status. Runner up: deriving status from `order_events` at read time (Option 4's cost).
- **Composite FK `(run_id, env)`.** The database itself rejects a demo fill on a live run, with no trigger code to maintain. Runner up: a trigger, which is more flexible but hidden logic.
- **`GIT_COMMIT` Docker build arg** as the commit source. Containers have no `.git`, and a build arg is explicit and testable. Runner up: reading `.git` at runtime, which fails in the runtime image.
- **`SECURITY DEFINER` downsample function.** Backup gets EXECUTE on one function instead of DELETE on tables, so a backup bug can't wipe history. Runner up: a DELETE grant on the snapshot tables only.
- **Sync Session for writes.** The recording actor writes from a background thread, and sync code is simpler to test. The dashboard's async reads belong to features 5 and 6. Runner up: async everywhere.
- **Conflict risk event in its own transaction, after the caller rolls back.** The repository never commits inside the caller's transaction, so a bundle still rolls back whole, and the alert is written only once the failed unit is gone. Its `event_key` hashes the incoming content, so a replayed conflict adds no second row. Runner up: a SAVEPOINT inside the caller's transaction, which either commits the caller's pending work early or loses the alert when the caller rolls back. (Changed after the cross check.)
- **Composite FKs on every parent link, not just runs.** Checking env only at the run level would still let a demo fill point at a live order. Every parent carries `UNIQUE (id, env)` so the database checks each link. Runner up: run level only, which leaves that hole. (Added after the cross check.)
- **`parent_client_order_id` as text, no FK.** A stop's event can arrive before its entry's, and an FK would reject it. The decision link keeps its FK because the strategy writes the decision before it submits orders. Runner up: a deferrable FK, which only helps when both rows arrive in one transaction.
- **Order lookup across runs for live and paper.** Live and paper start one run per process start, so orders placed before a restart belong to the previous run. Looking up `(env, client_order_id)` within the same `trader_id` stops every post restart fill from becoming a stub.
- **Live gate in `start_run`.** Feature 19 says live only after the gate passes, so the check belongs where a live run begins. A `manual` promotion row is the recorded escape hatch. Runner up: leaving it to feature 19's code, which can be bypassed by any new entry point.
