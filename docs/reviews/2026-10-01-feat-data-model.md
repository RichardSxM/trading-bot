# Review, feat/data-model, 2026-10-01

**Reviewed by**: Sonnet 5.5 (author on Opus 5.5; different model family)
**Scope**: 36 files, branch vs main (merge base 1dffd3a, data model slice c26386c..994ee86)
**Verdict**: Changes requested

## Summary
Adds the 18 table Postgres schema, role setup, the idempotent write API, run bundles, and the journal query for spec 0002. The structure is clean and the spec is followed closely: env composite FKs, CHECKs, guarded upserts, conflict reporting, and signed journal cash flows are all present and tested against a real Postgres. One performance problem sits on the order and position write hot path (an unindexable lookup predicate). The rest are minor edge and test gaps. Reviewed by reading only; I could not run the tests (Docker only).

## Major
### 🟠 Row lookups use `IS NOT DISTINCT FROM`, which Postgres cannot serve from a btree index, `packages/core/src/trading_core/recording/repository.py:115`
**Problem**: `_key_filter` builds every natural key lookup with `is_not_distinct_from`. `_stored` uses it on every `upsert_order` and `upsert_position` call (via `_guarded_upsert`, line 202) and on every replayed duplicate in `_insert_or_compare` (line 175). As far as I know, `col IS NOT DISTINCT FROM $1` is not an indexable btree operator, so each call is likely a sequential scan of `orders`, `positions`, `fills`, and so on. Please confirm with `EXPLAIN` on a seeded table.
**Why it matters**: Order updates are the hot path of a scalper, and the spec says sim runs grow these tables to gigabytes. Lookup cost then grows linearly with table size, on every order event, and again on every spill replay.
**Suggested fix**: Use plain `==` for key columns that are NOT NULL (every key except `account_snapshots.run_id`, which can use `IS NOT DISTINCT FROM` or an explicit `IS NULL` branch only when the value is None). Add a test that asserts the index is used, or at least covers the NULL `run_id` snapshot key.

## Minor
### 🟡 Run bundle does not check that its rows belong to the bundle's run, `packages/core/src/trading_core/recording/repository.py:658`
**Problem**: `write_run_bundle` inserts decisions, orders, fills, positions, and funding with whatever `run_id` and `env` each row carries. The docstring and spec assume "the run id is new, so nothing can conflict", but a row carrying another run's id (for example a live run in the same env) is accepted if it satisfies the FKs and uniques.
**Why it matters**: A simulator bug could add rows to a paper or live run, which the journal then counts as real trades.
**Suggested fix**: Before inserting, check every child row's `run_id` equals `bundle.run.id` and its `env` equals `bundle.run.env`. Raise a typed error otherwise.

### 🟡 `write_run_bundle` skips the env check entirely, so a demo process can write live tagged sim rows, `packages/core/src/trading_core/recording/repository.py:663`
**Problem**: Spec AC-4 excepts sim bundles, and `test_a_sim_bundle_of_the_other_env_lands_from_a_demo_process` pins it. Nothing stops a backtest bundle with `env = 'live'` from a demo process. The DB only forbids the reverse (live mode in demo).
**Why it matters**: It lets sim data sit in the live env's tables. The journal filters by mode, so money reports are safe, but dashboards reading by env would show it. Low risk, deliberate in the spec.
**Suggested fix**: Consider limiting the exception to `bundle.run.env == demo`, or document the live-tagged case as a conscious choice.

### 🟡 Settings.git_commit is never read by the write path, `packages/core/src/trading_core/recording/repository.py:251`
**Problem**: The spec's Value sourcing says `git_commit` comes from `Settings.git_commit`, but `ensure_strategy_version` takes it as a free parameter and nothing in core or the apps passes the setting. Verify step 11 is ticked, yet no code or test links the baked-in commit to the stored one.
**Why it matters**: A caller (feature 6) can record any commit string, or forget it, and a live gate that only rejects the literal `unknown` would accept it.
**Suggested fix**: Default the parameter from `get_settings().git_commit`, or note step 11 as deferred to feature 6 as was done for the decision tag.

### 🟡 The write API is never exercised as `trader_app`, `packages/core/tests/integration/conftest.py:151`
**Problem**: The `session` fixture connects as the owner. The role tests check grants in isolation, but no test runs the repository (UPDATE on orders, positions, strategy_runs, control_commands, fills, funding_payments; INSERT ... ON CONFLICT DO UPDATE) under `trader_app`.
**Why it matters**: A statement needing a privilege the role lacks only shows up in production, where the trader must fail closed.
**Suggested fix**: Add one integration test that runs the tracer thread plus an order update, a control outcome, and `finish_run` through a `trader_app` session.

### 🟡 Check then write is not atomic in `_guarded_upsert` and `set_control_outcome`, `packages/core/src/trading_core/recording/repository.py:202`
**Problem**: The immutable fields check reads the row, then the upsert runs later with no row lock. `set_control_outcome` likewise reads `command.details` and writes a merged copy (lines 597 to 614). Two concurrent writers can slip past the immutability check or lose a details merge.
**Why it matters**: Fine for the single trader process today. The spec's trader plus recorder or reconciliation writers could make it real.
**Suggested fix**: Use `SELECT ... FOR UPDATE` in those reads, or note the single writer assumption in the docstrings.

### 🟡 Late raw snapshots can end up as a second row in an already downsampled hour, `packages/core/src/trading_core/migrations/versions/20261001_0002_data_model.py:62`
**Problem**: Only `raw` rows are ranked. A raw row that arrives (or is replayed) later for an hour already thinned leaves an `hourly` row and a new row in the same bucket, and the next run marks the new one `hourly` too.
**Why it matters**: Breaks "one row per series per hour" slightly; spill replay older than 90 days is unlikely.
**Suggested fix**: Include existing `hourly` rows in the ranking, or document the assumption.

### 🟡 Fills and order events are not checked against their order, `packages/core/src/trading_core/recording/repository.py:462`
**Problem**: `record_fill` links a fill to the order found by `client_order_id` without comparing `side` or `instrument_id`, and the DB has no constraint tying them (the FK is `(order_id, env)` only).
**Why it matters**: A mapper bug records a sell fill on a buy order; the journal and position math then silently disagree.
**Suggested fix**: Compare side and instrument against the found order and raise `RecordConflictError`, or add it to the follow-up for reconciliation.

## Nits
- ⚪ `packages/core/src/trading_core/recording/repository.py:205`, `stored["status"] != OrderStatus.UNKNOWN` is also evaluated for positions (status `open`/`closed`), working only because `stub_only` is empty there; scope the stub rule to orders.
- ⚪ `packages/core/src/trading_core/db_roles.py:49`, passwords go out in `ALTER ROLE ... PASSWORD '<clear>'`, which appears in server logs if `log_statement` includes ddl; fine for dev, pre-hash (SCRAM) when feature 4 lands.
- ⚪ `packages/core/src/trading_core/migrations/versions/20261001_0002_data_model.py:55`, put `pg_temp` last in the SECURITY DEFINER `search_path` (`pg_catalog, public, pg_temp`).
- ⚪ `packages/core/src/trading_core/migrations/env.py:36`, offline mode no longer calls `configure_logging`, which the online path does.
- ⚪ `packages/core/src/trading_core/recording/queries.py:85`, the EUR division uses the default 28 digit context while the rest of the module uses the exact context; the spec says "full precision".
- ⚪ `packages/core/tests/integration/test_downsample.py:80`, the test commits its seeded rows into the session wide database, so later tests share them; harmless now.
- ⚪ `packages/core/tests/test_db_roles.py:48`, `"t" not in {str(error.get("input")) ...}` passes trivially because `include_input=False` drops the key; assert `"input" not in error` instead.

## Strengths
- The database enforces the rules (composite env FKs, live mode CHECK, strategy order needs decision, no float scan) and each is tested by trying to break it.
- The conflict path is thoughtful: compare by value and by `_ns`, `RecordConflictError` carries both contents, and `report_conflict` is idempotent on a content hash.
- The role setup is fail closed (one JSON line, exit 1) and the grants are tested exhaustively per role, table, and privilege.
- `journal_rows` keeps exact `Decimal` math and has well targeted edge tests (half open range, naive bounds, inverse instrument, rebate and zero fee).

## Test coverage
Strong for a configured runner: unit tests cover rows, enums vs Nautilus, models, settings, and role setup; integration tests cover AC-1 to AC-15 against real Postgres, including the migration round trip, downsample, bundle atomicity, and the live gate. Gaps: the repository is never run under `trader_app`, there is no test of the `account_snapshots` NULL `run_id` key path through `_stored`, no test of bundle rows with a foreign `run_id`, and no concurrency test (acceptable for now).
