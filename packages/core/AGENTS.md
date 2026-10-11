# trading-core (packages/core)

## Overview

The shared library every process imports: settings, JSON logging, the startup clock check, node naming, the env guard, health checks, and the SQLAlchemy metadata plus Alembic migrations. Pure domain and risk logic belongs here, free of I/O, per the root Clean Architecture rule.

## Stack

- Python 3.14, built with hatchling as `trading_core`
- pydantic-settings, SQLAlchemy 2, Alembic, psycopg 3
- Optional extra `node` pins `nautilus_trader==1.231.0` and names `pyarrow` and `numpy`; only trader, recorder, and lab install it

## Key files

| File | Owns |
|---|---|
| `src/trading_core/settings.py` | Non secret config from env vars, validated at startup; `secrets_dir` says where secret files live |
| `src/trading_core/secrets.py` | Secrets from mounted files only (spec 0003): `read_secret`/`load_required` (fail closed, fingerprint log, redaction registry), `redact`, the key age rule |
| `src/trading_core/node/key_check.py` | The live startup key check (spec 0003 AC-8): `key_check_state` and `watch_key_check`, run as a task on the trader's event loop; no Nautilus import |
| `src/trading_core/env_guard.py` | `check_environ` refuses any Kraken key variable in every process; `check_exec_clients` checks the execution client allowlist |
| `src/trading_core/boot.py` | Common process startup: settings, logs, env guard, secrets, config guard, clock check; returns `Boot(settings, secrets)` |
| `src/trading_core/db.py` | Shared metadata, constraint naming convention, and column type map (Decimal to numeric, datetime to timestamptz) |
| `src/trading_core/db_roles.py` | Creates or updates the `trader_app`, `dashboard_app`, `backup_app` roles before migrations (spec 0002) |
| `src/trading_core/recording/` | Postgres business records (spec 0002): `enums`, `rows` (frozen row values), `models` (18 tables), `errors`, `repository` (the idempotent write API), `queries` (`journal_rows`, the trade journal read) |
| `src/trading_core/control/` | The control command and result formats on the `{env}:control` and `{env}:control:results` Redis streams (spec 0004) |
| `src/trading_core/snapshot/` | The `{env}:snapshot:<name>` key and its JSON envelope (`schema_version`, `ts_ns`, `data`) that every snapshot writer and reader shares |
| `src/trading_core/migrations/` | Alembic env and versions (`alembic.ini` at package root) |
| `src/trading_core/catalog/` | The Parquet catalog adapter shared by recorder and lab (spec 0007, 0008): `BookCarry`, `HourSink`, `ReorderWindow`, interval naming, gap and manifest files; every write goes through `atomic_write`/`durable_replace` (fsync, then rename) |
| `src/trading_core/marketdata/` | Pure recorder logic (spec 0007): gaps, hours, the sequence tracker and holes file parsing, retention, queue depth, and the typed `CatalogError` family |
| `src/trading_core/node/` | Nautilus helpers (Kraken data and live execution client configs, heartbeat and key age actors); the only Nautilus imports in core |

## Commands

```bash
docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q
docker compose run --rm migrate                                     # role setup, then alembic upgrade head
docker compose run --rm -T migrate pytest -q -m integration         # integration tests on a throwaway database
```

## Conventions

- A new secret gets a name constant in `secrets.py`, a file in `scripts/dev-secrets.sh`, and a `secrets:` grant in `compose.yaml` for only the services that need it; load it with `load_required`, never from settings or env.
- Only `node/` and `catalog/` may import NautilusTrader, and only `catalog/` may import `pyarrow` or `numpy`; everything else in core stays importable without the `node` extra (spec 0008 Code placement, enforced by `tests/test_core_boundary.py`). The existing I/O adapters (`secrets`, `recording/`, `control/`, `db*`, `clock`, `health`, `logs`) may use files, `sqlalchemy`, `psycopg`, and `redis`.
- New tables go through `db.py`'s metadata so constraint names follow the naming convention.
- Every schema change ships as an Alembic migration.
- Writes go through `recording.repository`, never raw ORM inserts: it checks `env`, keeps writes idempotent, and raises `RecordConflictError` on a contradiction.
- Tests that need Postgres live in `tests/integration/` (auto marked `integration`, skipped by the default `pytest -q`).

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
