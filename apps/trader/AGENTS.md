# trader (apps/trader)

## Overview

The Nautilus node that holds positions: strategies, risk, and execution. The scaffold runs a node with no strategy and a public Kraken data client only. A recording actor will write orders, fills, and positions to Postgres.

## Stack

- Python 3.14, `trading-core[node]` (NautilusTrader 1.231.0, Kraken Futures adapter)
- Redis as the Nautilus cache database
- Entry point: `trader.main:main` (Compose runs `python -m trader.main`)

## Commands

```bash
docker compose build trader
docker compose run --rm --no-deps -T trader pytest -q
```

## Conventions

- The env guard runs before every node `build()`; execution clients must be on its allowlist.
- Redis cache keys stay stable across restarts (`trader-TRADER-<ENV>:...`), so the trader reloads state and then reconciles with Kraken. Never turn on `flush_on_start` or instance ids.
- The trader refuses to start when Redis is unreachable.
- Strategies stay thin; risk and sizing logic lives in `packages/core`.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
