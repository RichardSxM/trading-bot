# dashboard (apps/dashboard)

## Overview

The private web dashboard, reachable only over Tailscale. History comes from Postgres, live position and P&L from Redis `snapshot` keys pushed to the browser over SSE. The scaffold serves only `/healthz`; pages arrive with feature 5.

## Stack

- Python 3.14, FastAPI + uvicorn, HTMX + Server Sent Events, TradingView Lightweight Charts
- `trading-core` without the `node` extra
- Entry point: `dashboard.main:main`; dev tests use httpx

## Commands

```bash
docker compose build dashboard
docker compose run --rm --no-deps -T dashboard pytest -q
```

## Conventions

- Never import NautilusTrader, directly or through `trading_core.node`.
- Read only, except the `control` Redis stream, its single write path to the trader.
- Never receives Kraken keys. Its port binds to `127.0.0.1` only.
- API docs routes stay off (`docs_url=None`, `openapi_url=None`).

## Agent skills

- [fastapi](../../.claude/skills/fastapi/): `terminalskills/skills`, routes, dependencies, SSE
- [htmx](../../.claude/skills/htmx/): `damusix/skills`, hx attributes, swaps, SSE extension

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
