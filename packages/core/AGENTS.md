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
| `src/trading_core/settings.py` | Non secret config from env vars, validated at startup |
| `src/trading_core/env_guard.py` | Refuses unlisted execution clients and Kraken keys where none may exist |
| `src/trading_core/boot.py` | Common process startup |
| `src/trading_core/db.py` | Shared metadata and constraint naming convention |
| `src/trading_core/migrations/` | Alembic env and versions (`alembic.ini` at package root) |
| `src/trading_core/node/` | Nautilus helpers (Kraken data client, heartbeat actor); the only Nautilus imports in core |

## Commands

```bash
docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q
docker compose run --rm migrate
```

## Conventions

- Only `node/` may import NautilusTrader; everything else in core stays importable on the Intel Mac host.
- New tables go through `db.py`'s metadata so constraint names follow the naming convention.
- Every schema change ships as an Alembic migration.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
