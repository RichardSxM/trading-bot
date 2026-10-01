# 0002. Record trading in Postgres as traceable, idempotent, environment tagged tables

**Date**: 2026-10-01
**Status**: Proposed

## Summary

This spec designs the Postgres tables every later feature writes to and reads from: strategies and their versions, runs, decisions, orders, fills, positions, funding, balances, risk events, dashboard commands, and promotions. Market data stays in the Parquet catalog (spec 0001), so Postgres only holds business records. Every order traces back to the strategy decision and the exact code version that caused it, every write is safe to repeat, and demo and live rows can never mix. Feature 3 builds the schema, the models, and a write API in `packages/core`; feature 6 plugs the trader's recording actor into it.

## Requirements

**User stories**:
- As the trader, I want every order to trace to the decision, run, and exact strategy version behind it, so I can explain any trade and trust what the promotion gate approved.
- As the trader, I want paper, live, backtest, and replay results in one schema, so I can compare a strategy across them with plain SQL.
- As the trader, I want fills, fees, and funding stored exactly and kept forever, so my P&L and Dutch tax export are correct.
- As the operator, I want writes that are safe to retry and that fail loudly on contradictions, so a restart or spill replay never duplicates or silently rewrites a record.

**Acceptance criteria**:
- **AC-1**: `alembic upgrade head` on an empty database creates all 18 tables, their constraints and indexes, the role grants, and the downsample function; `alembic downgrade 0001_baseline` drops the tables, the function, and the grants (the roles stay, since role setup creates them outside Alembic); Alembic has exactly one head.
- **AC-2**: The database rejects an order with `origin = 'strategy'` and no `decision_id`; orders with origin `kill_switch`, `reconciliation`, or `external` may omit it.
- **AC-3**: One query joins any fill to its order, decision, run, and strategy version, returning the version's `git_commit` and `config`.
- **AC-4**: Every env scoped table has `env` limited to `demo` or `live`; the database rejects any row whose `env` differs from the `env` of a parent it references (run, decision, order, or position), and a run with `mode = 'live'` unless `env = 'live'`; the write API rejects a row whose `env` differs from `Settings.trading_env` (sim bundles excepted).
- **AC-5**: Writing the same fill, order event, funding payment, decision, risk event, snapshot, or control command twice through the write API leaves one row and raises nothing.
- **AC-6**: Writing a record whose natural key exists with different content raises `RecordConflictError` and keeps the original row unchanged; `report_conflict` then commits one `risk_events` row with kind `record_conflict` and severity `critical` in its own transaction, and reporting the same conflict again adds no row.
- **AC-7**: Recording a fill whose order cannot be found creates a stub order (`origin = 'external'`, `status = 'unknown'`, `last_event_ns = 0`) and the fill in one transaction; a later `upsert_order` for that order fills in the stub.
- **AC-8**: `upsert_order` and `upsert_position` apply an update only when its `last_event_ns` is greater than or equal to the stored one; an older update is ignored without error.
- **AC-9**: Prices, quantities, fees, P&L, balances, and rates round trip as exact `Decimal` (for example `0.00000001` and `98765.4321`); every money amount has a currency; every event time is stored as UTC `timestamptz` plus a `bigint` nanosecond value; no column is a float type.
- **AC-10**: `write_run_bundle` writes a finished backtest or replay (run, results, decisions, orders, events, fills, positions, funding) in one transaction; an error partway leaves no row of that run; a bundle with mode `paper` or `live` is rejected.
- **AC-11**: The `dashboard_app` role can SELECT every table and cannot INSERT, UPDATE, or DELETE; `trader_app` can SELECT and INSERT everywhere, UPDATE only the tables listed in the Security model, and cannot DELETE or run DDL; `backup_app` can SELECT and execute `downsample_snapshots()` but cannot DELETE directly; no other role can execute `downsample_snapshots()`.
- **AC-12**: `downsample_snapshots()` reduces `raw` position and account snapshots older than 90 days to the last row per series per UTC hour, marked `granularity = 'hourly'`, leaves newer rows untouched, and changes nothing on a second run.
- **AC-13**: The role setup step creates or updates the three roles idempotently, and the migrate service refuses to start (exit 1, one JSON error log) when any role password variable is missing or empty.
- **AC-14**: `journal_rows(session, env, start, end)` returns every fill (with fee) and funding payment from `paper` and `live` runs with time in `[start, end)` UTC, each with its native currency and a EUR amount from `fx_rates` (or a NULL EUR amount and `fx_missing = true` when no rate exists).
- **AC-15**: `record_promotion` refuses a promotion with no evidence run or with an evidence run of another version, and `start_run` with `mode = 'live'` refuses a version whose latest `paper → live` promotion is missing or not `pass`, or whose `git_commit` is `unknown`.

## Decision

**Chosen option**: Option 2: normalized operational schema with current rows plus append only events, written through an idempotent core repository.

Postgres holds 18 normalized tables keyed by UUIDv7, with natural UNIQUE keys for idempotent writes, an `env` column enforced through composite foreign keys on every parent link, and a typed write API in `trading_core.recording`; market data stays in Parquet.

**Implementation skills**: `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `alembic` (`terminalskills/skills`, `.claude/skills/alembic/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Feature design

**Conventions (all tables)**:
- Primary key `id uuid`, set in Python with `uuid.uuid7()` (time ordered), except `instruments` (text key), `fx_rates` (composite natural key), `run_results` (`run_id, sample`), `promotion_evidence` (composite), and `control_commands` (the command's own UUID).
- Money and sizes are `numeric` with no precision or scale (exact, any precision Nautilus uses), mapped to `Decimal`; a `*_currency text` column sits beside each money amount (one column may serve several amounts in the same currency).
- Event times: `<name>_ns bigint NOT NULL` (the Nautilus nanosecond value) is the truth; `<name>_at timestamptz NOT NULL` is derived from it in the row factory (microsecond precision). Bookkeeping: `created_at timestamptz NOT NULL DEFAULT now()`, plus `updated_at` on mutable rows.
- Signs: `fee` is positive when paid, negative for a rebate; funding `amount` is negative when paid; `realized_pnl` is net of fees and funding; `positions.quantity` is signed (positive long, negative short); returns, drawdown, and win rate are fractions (`0.05` means 5 %).
- JSONB (`inputs`, `config`, `run_config`, `details`, `thresholds`) stores `Decimal` as strings and times as ns integers, serialized with sorted keys, so `version_hash` is stable.
- Enums are `text` with a named CHECK listing the values, mirrored by `StrEnum`s in `trading_core.recording.enums`. Nautilus enums (`OrderSide`, `OrderType`, `TimeInForce`, `OrderStatus`, `LiquiditySide`, order event class names) are stored as their names in snake case (`buy`, `stop_market`, `gtc`, `partially_filled`, `no_liquidity`, `order_filled`); the CHECK lists every member of the pinned Nautilus 1.231.0 enum, plus `unknown` for order status.
- Every env scoped table has `env text NOT NULL CHECK (env IN ('demo','live'))`. Every parent it references carries `UNIQUE (id, env)`, and the child references it with a composite FK on `(<parent>_id, env)`. So a row can never point at a parent in the other env. This applies to `strategy_runs`, `decisions`, `orders`, and `positions` as parents.
- Constraint names come from `trading_core.db.NAMING_CONVENTION`.

**Data model sketch**:

Global tables (no `env`):

| Table | Key and uniques | Columns (NULL means nullable; all others NOT NULL) | Links |
|---|---|---|---|
| `instruments` | PK `instrument_id text` (Nautilus InstrumentId, `PF_XBTUSD.KRAKEN`) | venue, symbol, kind (`perpetual`), base_currency, quote_currency, settle_currency, tick_size, lot_size, multiplier, is_inverse bool, created_at | |
| `strategies` | UNIQUE name | name, kind (`scalping`,`hour_level`), description NULL, created_at | 1:N versions |
| `strategy_versions` | UNIQUE version_hash | strategy_id, git_commit, config jsonb, version_hash (sha256 hex of canonical JSON `{git_commit, config}`), created_at | N:1 strategies |
| `promotions` | | strategy_version_id, from_stage, to_stage (`candidate`,`paper`,`live`), verdict (`pass`,`fail`), thresholds jsonb, decided_by (`gate`,`manual`), notes NULL, decided_at | N:1 versions |
| `promotion_evidence` | PK (promotion_id, run_id) | promotion_id, run_id | N:M promotions ↔ runs |
| `fx_rates` | PK (rate_date, base_currency, quote_currency) | rate_date date, base_currency (`EUR`), quote_currency (`USD`), rate (USD per 1 EUR, as ECB quotes it), source (`ECB`), created_at | |

Env scoped tables:

| Table | Key and uniques | Columns | Links |
|---|---|---|---|
| `strategy_runs` | UNIQUE (id, env) | env, mode (`backtest`,`replay`,`paper`,`live`), status (`running`,`completed`,`failed`,`aborted`), strategy_version_id, trader_id (`TRADER-DEMO`, or the backtest engine's id), run_config jsonb (sim settings: balance, fee, latency, fill model), data_start_at NULL, data_end_at NULL, started_at, ended_at NULL; CHECK `mode <> 'live' OR env = 'live'` | N:1 versions |
| `run_results` | PK (run_id, sample) | run_id, env, sample (`in_sample`,`out_of_sample`,`full`), return_after_costs, max_drawdown, risk_adjusted_ratio NULL, ratio_kind NULL, win_rate, trade_count int, fees_total, funding_total, currency, computed_at | N:1 runs (one per sample) |
| `decisions` | UNIQUE (id, env) | env, run_id, instrument_id, intent (`enter`,`exit`,`adjust`), side (`buy`,`sell`) NULL, reason text, inputs jsonb, risk_verdict (`approved`,`rejected`), risk_reason NULL, decided_at, decided_ns | N:1 runs, instruments |
| `orders` | UNIQUE (run_id, client_order_id); UNIQUE (id, env) | env, run_id, decision_id NULL, origin (`strategy`,`kill_switch`,`reconciliation`,`external`), client_order_id, venue_order_id NULL, parent_client_order_id NULL (text, no FK: the entry a stop belongs to), order_list_id NULL, instrument_id, side, order_type, time_in_force, quantity, price NULL, trigger_price NULL, reduce_only bool, post_only bool, status, filled_qty, avg_px NULL, last_event_ns bigint, created_at, updated_at; CHECK `origin <> 'strategy' OR decision_id IS NOT NULL` | N:1 decisions, runs, instruments |
| `order_events` | UNIQUE (order_id, event_id) | env, order_id, event_id uuid (Nautilus event id), kind, details jsonb, occurred_at, occurred_ns | N:1 orders |
| `fills` | UNIQUE (run_id, trade_id) | env, run_id, order_id, position_id NULL, instrument_id, trade_id, side, quantity, price, fee, fee_currency, liquidity (`maker`,`taker`,`no_liquidity`), filled_at, filled_ns | N:1 orders, positions |
| `positions` | UNIQUE (run_id, position_ref, opened_ns); UNIQUE (id, env) | env, run_id, instrument_id, position_ref (Nautilus PositionId), side (`long`,`short`,`flat`), status (`open`,`closed`), quantity (signed), peak_qty, avg_entry_px, avg_exit_px NULL, realized_pnl, fees_total, funding_total, currency, opened_at, opened_ns, closed_at NULL, last_event_ns, updated_at | N:1 runs, instruments |
| `position_snapshots` | UNIQUE (position_id, captured_ns) | env, position_id, quantity, mark_px, unrealized_pnl, liquidation_px NULL, currency, granularity (`raw`,`hourly`), captured_at, captured_ns | N:1 positions |
| `account_snapshots` | UNIQUE NULLS NOT DISTINCT (env, run_id, account_ref, currency, captured_ns) | env, run_id NULL, account_ref (Nautilus AccountId), balance_total, balance_free, margin_used, currency, granularity, captured_at, captured_ns | N:1 runs |
| `funding_payments` | partial UNIQUE (env, external_id) WHERE source = 'kraken_log'; partial UNIQUE (run_id, instrument_id, paid_ns) WHERE source = 'computed' | env, run_id, position_id NULL, instrument_id, amount, currency, rate NULL, source (`kraken_log`,`computed`), external_id NULL (Kraken log id), paid_at, paid_ns; CHECK `source <> 'kraken_log' OR external_id IS NOT NULL` | N:1 runs, positions |
| `risk_events` | UNIQUE (env, event_key) | env, event_key text, kind (`order_rejected`,`limit_hit`,`halt`,`resume`,`stale_feed`,`reconcile_mismatch`,`record_conflict`,`orphan_fill`), severity (`info`,`warning`,`critical`), message, details jsonb, run_id NULL, decision_id NULL, order_id NULL, occurred_at, occurred_ns | optional N:1 runs, decisions, orders |
| `control_commands` | PK id (the command message's `id` UUID field, not the Redis stream entry id) | env, cmd (`stop`,`resume`,`flatten`), issued_at, expires_at, received_at, outcome (`received`,`executed`,`expired`,`failed`,`duplicate`), outcome_at NULL, details jsonb | |

Nullable parent links (`run_id` on `account_snapshots` and `risk_events`, `position_id`, `decision_id`, `order_id` on `risk_events`) use the same composite FKs; with a NULL id Postgres skips the check, which is the intended meaning (no parent).

Indexes beyond the uniques: `orders (env, status) WHERE status NOT IN ('denied','rejected','canceled','expired','filled')` (open orders; `unknown`, `emulated`, and `released` count as open), `orders (env, client_order_id)`, `fills (env, filled_at)`, `funding_payments (env, paid_at)`, `decisions (run_id, decided_at)`, `positions (env, status)`, `position_snapshots (position_id, captured_at)`, `account_snapshots (env, account_ref, captured_at)`, `risk_events (env, occurred_at)`, `strategy_runs (strategy_version_id, mode)`, `strategy_runs (env, trader_id, status)`. Every FK column gets an index.

**State transitions**:
- `strategy_runs.status`: `running → completed | failed | aborted` (terminal; `ended_at` is set on leaving `running`). Live and paper: one run per process start. `start_run` first marks any `running` run with the same `(env, trader_id)` as `aborted` (a crash left it open).
- `orders.status`: the Nautilus `OrderStatus` members plus `unknown` (stub). Nautilus owns the transitions; the database stores the latest one, guarded by `last_event_ns`. `unknown` may move to any status.
- `positions.status`: `open → closed`. A reopened NETTING position is a new row (new `opened_ns`).
- `control_commands.outcome`: `received → executed | expired | failed | duplicate` (`duplicate` means the trader saw this id again after it was already handled). Setting the same final outcome again is a no-op; changing a final outcome raises.
- `fills`, `order_events`, `decisions`, `funding_payments`, `risk_events`, `promotions`, `run_results`: immutable once written, except `fills.position_id` and `funding_payments.position_id`, which may go once from NULL to a value. Snapshots change only through downsampling.

**API surface** (Python, `trading_core.recording.repository`; every function takes a sync SQLAlchemy `Session` and a frozen row dataclass from `trading_core.recording.rows`; the caller owns commit and rollback, except `write_run_bundle` and `report_conflict`, which commit themselves):

| Function | Key inputs | Returns | Key errors |
|---|---|---|---|
| `upsert_instrument` | `InstrumentRow` | None (insert if new, no-op if equal) | `RecordConflictError` if any spec differs (instrument changes need a deliberate migration step, so the trader fails closed) |
| `ensure_strategy_version` | name, kind, git_commit, config | version `UUID` (existing or new; idempotent on hash) | `RecordConflictError` if the name exists with another kind |
| `start_run` | env, mode, version_id, trader_id, run_config, data range, started_ns | run `UUID` | `PromotionRequiredError`, `UnknownCommitError` (live only) |
| `finish_run` | run_id, status, ended_ns | None | `InvalidTransitionError` if not `running` |
| `record_run_result` | `RunResultRow` | None | `RecordConflictError` |
| `record_decision` | `DecisionRow` (id set by the strategy) | None | `RecordConflictError` |
| `upsert_order` | `OrderRow` | None (ignored when older by `last_event_ns`) | `RecordConflictError` if `side`, `instrument_id`, or `client_order_id` change; `origin`, `decision_id`, and `order_type` may change only while the stored status is `unknown` |
| `record_order_event` | `OrderEventRow` (carries `run_id` and `client_order_id`) | None | `MissingParentError` if the order is not found, `RecordConflictError` |
| `record_fill` | `FillRow` (carries `client_order_id`, not `order_id`) | None; stub order plus `orphan_fill` risk event when the order is not found | `RecordConflictError` |
| `upsert_position` | `PositionRow` | None (guarded by `last_event_ns`) | `RecordConflictError` if `instrument_id`, `position_ref`, or `opened_ns` change |
| `record_position_snapshot` | `PositionSnapshotRow` | None | `MissingParentError`, `RecordConflictError` |
| `record_account_snapshot` | `AccountSnapshotRow` | None | `RecordConflictError` |
| `record_funding` | `FundingRow` | None | `RecordConflictError` |
| `record_risk_event` | `RiskEventRow` | None | `RecordConflictError` |
| `record_control_command` / `set_control_outcome` | row / id, outcome, outcome_ns, details | None | `InvalidTransitionError`, `RecordConflictError` |
| `record_promotion` | `PromotionRow`, evidence run ids | promotion `UUID` | `MissingEvidenceError` (no runs, or a run of another version) |
| `report_conflict` | a separate `Session`, the caught `RecordConflictError` | None; commits one risk event | none (idempotent on `event_key`) |
| `write_run_bundle` | `RunBundle` (version id, run, results, decisions, orders, order events, fills, positions, funding) | run `UUID`; commits | `InvalidModeError` for `paper` or `live`; rolls back everything on any error |
| `journal_rows` (`trading_core.recording.queries`) | env, start, end | rows: kind (`fill`,`fee`,`funding`), at, run_id, instrument_id, amount, currency, eur_amount NULL, fx_missing | |
| `downsample_snapshots()` | SQL function, no args | rows deleted (int) | |

**Write rules**:
- **Env check.** Every function except `write_run_bundle` raises `EnvMismatchError` when `row.env != Settings.trading_env`. Sim runs (backtest, replay) use the env of the process that runs them, which is `demo` on the laptop.
- **Idempotent insert.** `INSERT … ON CONFLICT (<the table's one natural key>) DO NOTHING RETURNING id`. Each table has exactly one natural key used as the arbiter (for `funding_payments`, the partial unique that matches the row's `source`). When nothing returns, the function reads the stored row and compares content: every column except `id`, `created_at`, and `updated_at`; `Decimal` compared by value; times compared by `_ns`; `position_id` NULL in either row counts as equal (and a NULL stored value is then set from the incoming one). Equal is a no-op; different raises `RecordConflictError` carrying the table, natural key, and both contents.
- **Guarded upsert** (`orders`, `positions`): `INSERT … ON CONFLICT DO UPDATE SET … WHERE stored.last_event_ns <= excluded.last_event_ns`; immutable fields are checked first (table above).
- **Order lookup** for fills and events: the order with this `client_order_id` in the given run; when none exists and the run is `paper` or `live`, the latest order with this `(env, client_order_id)` in any earlier run of the same `trader_id` (orders survive restarts). Only when both miss does `record_fill` create a stub.
- **Stub order**: `origin = 'external'`, `status = 'unknown'`, `decision_id` NULL, `side`, `instrument_id`, and `run_id` from the fill, `quantity = filled_qty =` the fill's quantity, `order_type = 'market'`, `time_in_force = 'gtc'`, `reduce_only = post_only = false`, `last_event_ns = 0` (so any real order update wins), plus an `orphan_fill` risk event with severity `warning`.
- **Conflict reporting**: the repository never commits inside a caller's transaction. The caller catches `RecordConflictError`, rolls back, calls `report_conflict` with a fresh session, then fails closed (feature 6 blocks new entries). `report_conflict` writes kind `record_conflict`, severity `critical`, `event_key = record_conflict:{table}:{natural key}:{sha256 of the incoming content}`, env from `Settings.trading_env`.
- **Bundles**: `write_run_bundle` uses plain batched inserts (the run id is new, so nothing can conflict) in FK order: run, results, decisions, orders, order events, positions, fills, funding.

**Downsample function** (`downsample_snapshots()`, PL/pgSQL, `SECURITY DEFINER`, owner `trading`, `SET search_path = pg_catalog, public`): cutoff `now() - interval '90 days'`; series are `position_id` for positions and `(env, run_id, account_ref, currency)` for accounts; buckets are `date_trunc('hour', captured_at)` in UTC; only `raw` rows before the cutoff are considered; in each bucket the row with the highest `captured_ns` is kept (its values and `captured_ns` unchanged) and set to `hourly`; the other `raw` rows in that bucket are deleted. Returns the delete count. A second run finds no `raw` rows to thin, so it changes nothing.

**Value sourcing**:

| Action | Value | Source |
|---|---|---|
| any write | `env` | `Settings.trading_env`, checked against the row |
| any write | `id` | `uuid.uuid7()` in `rows.py` factories (a decision's id is created by the strategy before it submits orders) |
| any write | `<name>_at` | derived from `<name>_ns` in the row factory |
| `ensure_strategy_version` | `git_commit` | `Settings.git_commit` from env `GIT_COMMIT`, baked into the image by the `GIT_COMMIT` Docker build arg; default `unknown` |
| `ensure_strategy_version` | `version_hash` | sha256 of canonical JSON (sorted keys, no spaces, Decimal as string) of `{git_commit, config}` |
| `start_run` | `trader_id`, `started_ns` | the node's trader id from `trading_core.naming` and the node clock (live and paper); the backtest engine's id and clock (sim) |
| `start_run` (live) | promotion check | the latest `promotions` row by `decided_at` with `from_stage = 'paper'`, `to_stage = 'live'` for that version; it must be `pass` |
| `orders.decision_id` | | the decision id the strategy puts in the Nautilus order's `tags` as `decision:<uuid>`; the feature 6 mapper reads it back |
| orders, fills, events, positions | ids, prices, sizes, fees, statuses, ns timestamps | the Nautilus event fields, mapped by feature 6 (live, paper) and features 13 and 14 (sim) |
| `fills.position_id` | | the mapper's lookup by `(run_id, position_ref, opened_ns)`; NULL if not yet known, set later by the NULL to value rule |
| `funding_payments` (`kraken_log`) | amount, external_id, paid_ns | Kraken Futures account log (feature 6 or 10 owns the poller) |
| `funding_payments` (`computed`) | amount, rate | the simulator (features 13, 14), which owns where funding rates come from |
| `run_results` | metrics, `ratio_kind` | computed by feature 15's scorer; this spec only stores them |
| `fx_rates` | rate | ECB daily reference rate (USD per EUR), fetched by feature 18 |
| `journal_rows` | `eur_amount` | `amount ÷ rate`, where rate is the `fx_rates` row for the event's UTC date or the latest earlier date (weekends, holidays); USD only; other currencies and missing rates give NULL plus `fx_missing = true` |
| `instruments` | specs | the Nautilus instrument provider, upserted by the trader at startup (feature 6) and by the backtest runner |
| snapshots | when to write | the caller (feature 6 actor): on change, at most 1/s per series, plus every 60 s |
| `control_commands` | `received_at`, `outcome_at` | the trader's clock when it reads the command and when it finishes handling it |
| `risk_events.event_key` | | caller supplied and deterministic for the event (for example `halt:{run_id}:{occurred_ns}`); `orphan_fill:{run_id}:{trade_id}`; `record_conflict:` as above |
| role setup | passwords | `POSTGRES_TRADER_PASSWORD`, `POSTGRES_DASHBOARD_PASSWORD`, `POSTGRES_BACKUP_PASSWORD` |

**Key invariants**:
- An order with origin `strategy` always has a decision (CHECK).
- A row's `env` equals the `env` of every parent it references (composite FKs); live runs only in live env (CHECK).
- A live run's version passed `paper → live` most recently and has a known commit (enforced in `start_run`).
- Immutable tables are never updated or deleted by application code; `trader_app` has no UPDATE on them except `fills` and `funding_payments`, where the repository only ever sets a NULL `position_id`.
- `orders` and `positions` never move backwards in `last_event_ns`.
- No `float`, `real`, or `double precision` column anywhere (a test scans the metadata).
- Market data never lands in Postgres.

**Security model**:
- Roles (all `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`, each granted `CONNECT` on the database and `USAGE` on schema `public`): `trading` (existing owner, used only by `migrate` and integration tests), `trader_app`, `dashboard_app`, `backup_app`.
- Table level grants: SELECT on all tables to all three roles; INSERT on all tables to `trader_app`; UPDATE to `trader_app` only on `orders`, `positions`, `strategy_runs`, `control_commands`, `fills`, and `funding_payments` (snapshots get none; downsampling runs as the owner). No DELETE or TRUNCATE for any app role. `ALTER DEFAULT PRIVILEGES` gives future tables SELECT for all roles and INSERT for `trader_app`; UPDATE is always granted per table.
- `downsample_snapshots()`: `REVOKE ALL … FROM PUBLIC`, then `GRANT EXECUTE` to `backup_app` only.
- `trader` and `dashboard` connect with their own role through `POSTGRES_USER` and `POSTGRES_PASSWORD` in their Compose environment. The recorder has no Postgres access.
- No secrets in any table. `inputs`, `details`, and `run_config` JSONB must never hold API keys (the mappers build them from allowlisted fields).
- Financial records are kept forever (Dutch retention rules want 7 years); only snapshots are thinned.
- Real password handling moves to feature 4; until then the dev values live in the gitignored `.env`.

**Configuration required**:
- `POSTGRES_TRADER_PASSWORD`, `POSTGRES_DASHBOARD_PASSWORD`, `POSTGRES_BACKUP_PASSWORD`: role passwords, read by the migrate role setup (dev values in `.env.example`); Compose also maps the first two to `POSTGRES_PASSWORD` for `trader` and `dashboard`.
- `GIT_COMMIT`: the commit the image was built from (Docker build arg and runtime env, default `unknown`); a new `Settings.git_commit` field.

**Critical test scenarios**:
- Happy path tracer: insert instrument → version → run → decision → strategy order → fill → position, then the AC-3 join returns the commit and config, verifies **AC-3**, **AC-9**.
- Replay the same fill, event, snapshot, and command twice → one row each, no error, verifies **AC-5**.
- Same `trade_id`, different price → `RecordConflictError`, original kept; `report_conflict` twice → one critical risk event, verifies **AC-6**.
- Fill before order → stub order, then `upsert_order` completes it; a fill after a trader restart finds the order from the previous run, no stub, verifies **AC-7**.
- `filled` then an older `accepted` update → status stays `filled`; the same update twice → no change, verifies **AC-8**.
- A strategy order without a decision, a fill whose env differs from its order, a live run in demo, a `live` row written by a demo process → errors, verifies **AC-2**, **AC-4**.
- Bundle with a bad row in the middle → no rows for that run; a `paper` bundle → `InvalidModeError`, verifies **AC-10**.
- Connect as each role: `dashboard_app` INSERT denied; `trader_app` DELETE denied, UPDATE on `decisions` denied; `backup_app` DELETE denied, `SELECT downsample_snapshots()` ok; `trader_app` executing it denied, verifies **AC-11**.
- Seed 100 days of snapshots → old rows one per hour per series, recent rows untouched, a second run deletes 0, verifies **AC-12**.
- Migrate with `POSTGRES_DASHBOARD_PASSWORD` unset → exit 1; run role setup twice → no error, verifies **AC-13**.
- Journal over a weekend with Friday's rate only → EUR uses Friday; a date before any rate → `fx_missing`; backtest fills excluded, verifies **AC-14**.
- Live `start_run` with no promotion, with a `pass` followed by a later `fail`, with commit `unknown` → refused; a promotion citing another version's run → `MissingEvidenceError`, verifies **AC-15**.
- Metadata scan finds no float columns; `0.00000001` round trips, verifies **AC-9**.
- `upgrade`, `downgrade 0001_baseline`, `upgrade` again → same schema; roles still exist after downgrade, verifies **AC-1**.

## Build plan

Tracer Bullet within one migration: land the whole schema (it is a foundation and fully designed), then prove one thin traceable write path end to end before widening the write API.

**Milestone 1: schema and roles**
1. `trading_core.recording` package: `enums.py` (StrEnums, Nautilus members listed by hand so the package needs no Nautilus import), `rows.py` (frozen dataclasses with `uuid7` and `_at` from `_ns` factories, `Decimal` and aware datetimes only, canonical JSON helper), `errors.py` (`RecordingError` and subclasses), satisfies **AC-9**, **AC-6**.
2. Settings: `git_commit`; a separate `RoleSettings` for the three role passwords (refuses when empty); `database_url` escapes user and password with `urllib.parse.quote`; `ARG GIT_COMMIT` placed after the dependency install layer in `docker/Dockerfile` and passed in every app's Compose build args, satisfies **AC-13**, **AC-15**.
3. `trading_core/db_roles.py`: idempotent `CREATE ROLE` or `ALTER ROLE … PASSWORD` built with `psycopg.sql.Literal` (DDL takes no bind parameters), plus `CONNECT` and `USAGE`; the migrate command becomes `sh -c "python -m trading_core.db_roles && alembic upgrade head"`; `trader` gains a Postgres env block and `trader` and `dashboard` set `POSTGRES_USER` to `trader_app` and `dashboard_app` with their own password variables (the shared `*postgres-env` anchor stays for migrate); `.env.example` gains the dev passwords, satisfies **AC-13**, **AC-11**.
4. SQLAlchemy models in `trading_core/recording/models.py` on `trading_core.db.Base` for all 18 tables, with the checks, composite FKs, uniques (including partial and `NULLS NOT DISTINCT`), and indexes above, satisfies **AC-1**, **AC-2**, **AC-4**, **AC-9**.
5. Migration `0002_data_model` (autogenerate, then checked by hand): tables, grants, default privileges, `downsample_snapshots()` with its revoke and grant; `downgrade` drops all of them and leaves the roles, satisfies **AC-1**, **AC-11**, **AC-12**.

**Milestone 2: traceable write thread**
6. Repository thin thread: the shared insert, compare, and guarded upsert helpers, the env check, `upsert_instrument`, `ensure_strategy_version`, `start_run` (with the stale run sweep), `finish_run`, `record_decision`, `upsert_order`, `record_fill` (order lookup and stub), `upsert_position`, `report_conflict`, satisfies **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**.
7. Integration test setup: tests marked `integration` run in the `migrate` service (`docker compose run --rm -T migrate pytest -q -m integration`, dev image target, on the Compose network as owner `trading`); a session fixture creates `trading_test_<random hex>`, runs role setup and `alembic upgrade head` into it, and drops it at the end (roles are cluster wide and shared with dev, which is fine since setup is idempotent); the tracer test, satisfies **AC-1**, **AC-3**.

**Milestone 3: full write API**
8. The remaining functions: order events, snapshots, funding, risk events, control commands, run results, promotions, and the live gate in `start_run`, satisfies **AC-5**, **AC-15**.
9. `write_run_bundle` in one transaction, satisfies **AC-10**.
10. `journal_rows` in `trading_core.recording.queries`, satisfies **AC-14**.

**Milestone 4: guards and retention**
11. Role permission tests (connect as each role), the metadata test that rejects float columns, the downsample tests, the migration round trip test, satisfies **AC-11**, **AC-12**, **AC-9**, **AC-1**.
12. Update `packages/core/AGENTS.md` key files for `recording/` and `db_roles.py`, and its command list for the integration tests, satisfies **AC-1**.

## Consequences

**Positive**:
- Any trade explains itself: fill → order → decision with inputs → run → version with commit and config.
- Paper, live, and sim results share one schema, so features 15 and 18 are SQL over existing tables.
- At least once delivery, spill replay, restarts, and reconciliation are safe by construction, and contradictions surface as critical risk events instead of silent rewrites.
- The database itself enforces env isolation on every link, decision traceability, and the dashboard's read only role.

**Negative / tradeoffs**:
- 18 tables before the first trade: more migration and test surface than feature 6 strictly needs.
- `orders` and `positions` duplicate state that `order_events` and fills also imply; the `last_event_ns` guard keeps them consistent, but they can still drift if a mapper is wrong (feature 11's reconciliation should compare).
- Composite FKs carry `env` into every child key and add a `UNIQUE (id, env)` index per parent table.
- Large replays put many simulated fills in Postgres; table growth comes from sim runs, not trading. Revisit if it passes a few GB (a Follow-up).
- The live promotion gate in `start_run` means a live run cannot start until a promotion exists; in early live testing a `manual` promotion row is the escape hatch, and it is recorded.
- A changed instrument spec stops the trader at startup until someone runs a deliberate update; safe, but a Kraken tick size change becomes an incident.
- Downsampling does nothing until feature 4 makes `backup` real; before then snapshots grow unthinned (fine for the first months).
- Integration tests share the cluster's roles with the dev stack.

**Neutral**:
- `uuid.uuid7()` needs Python 3.14 (already pinned).
- Role passwords move to feature 4's secret store later without schema change.

## Follow-up

- [ ] Feature 4: have `backup` call `SELECT downsample_snapshots()` nightly before `pg_dump`, and move the three role passwords into the secret store.
- [ ] Feature 6: build the recording actor and the Nautilus event to row mappers on this write API, put `decision:<uuid>` in order tags, and decide who polls Kraken's account log for funding.
- [ ] Feature 11: compare `orders` and `positions` with the exchange during reconciliation; decide how an instrument spec change is applied.
- [ ] Feature 15: define `risk_adjusted_ratio` and `ratio_kind`, and the in sample and out of sample windows.
- [ ] Feature 18: the ECB rate fetcher that fills `fx_rates`.
- [ ] Revisit sim fill volume once replays run: if sim tables pass a few GB, move sim fills to Parquet per run (Option 3 in rationale).
- [ ] `/scope`: feature 3's line lists market data (trades, book snapshots, candles) as Postgres records; this spec keeps market data in Parquet per spec 0001.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
