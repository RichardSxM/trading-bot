# trading-core (packages/core)

## Overview

The shared library every process imports: settings, JSON logging, the startup clock check, node naming, the env guard, health checks, and the SQLAlchemy metadata plus Alembic migrations. Pure domain and risk logic belongs here, free of I/O, per the root Clean Architecture rule.

## Stack

- Python 3.14, built with hatchling as `trading_core`
- pydantic-settings, SQLAlchemy 2, Alembic, psycopg 3
- Optional extra `node` pins `nautilus_trader==1.231.0`; only trader and recorder install it

## Key files

| File | Owns |
|---|---|
| `src/trading_core/settings.py` | Non secret config from env vars, validated at startup; `secrets_dir` says where secret files live |
| `src/trading_core/secrets.py` | Secrets from mounted files only (spec 0003): `read_secret`/`load_required` (fail closed, fingerprint log, redaction registry), `redact`, the key age and startup key check rules |
| `src/trading_core/env_guard.py` | `check_environ` refuses any Kraken key variable in every process; `check_exec_clients` checks the execution client allowlist |
| `src/trading_core/boot.py` | Common process startup: settings, logs, env guard, secrets, config guard, clock check; returns `Boot(settings, secrets)` |
| `src/trading_core/db.py` | Shared metadata, constraint naming convention, and column type map (Decimal to numeric, datetime to timestamptz) |
| `src/trading_core/db_roles.py` | Creates or updates the `trader_app`, `dashboard_app`, `backup_app` roles before migrations (spec 0002) |
| `src/trading_core/recording/` | Postgres business records (spec 0002): `enums`, `rows` (frozen row values), `models` (18 tables), `errors`, `repository` (the idempotent write API), `queries` (`journal_rows`, the trade journal read) |
| `src/trading_core/migrations/` | Alembic env and versions (`alembic.ini` at package root) |
| `src/trading_core/node/` | Nautilus helpers (Kraken data and live execution client configs, heartbeat and key age actors); the only Nautilus imports in core |

## Commands

```bash
docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q
docker compose run --rm migrate                                     # role setup, then alembic upgrade head
docker compose run --rm -T migrate pytest -q -m integration         # integration tests on a throwaway database
```

## Conventions

- Only `node/` may import NautilusTrader; everything else in core stays importable on the Intel Mac host.
- New tables go through `db.py`'s metadata so constraint names follow the naming convention.
- Every schema change ships as an Alembic migration.
- Writes go through `recording.repository`, never raw ORM inserts: it checks `env`, keeps writes idempotent, and raises `RecordConflictError` on a contradiction.
- Tests that need Postgres live in `tests/integration/` (auto marked `integration`, skipped by the default `pytest -q`).

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
