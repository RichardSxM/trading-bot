# recorder (apps/recorder)

## Overview

A separate Nautilus node with data clients only. It records L2 book deltas and trades for the BTC and ETH perps through Nautilus's streaming (feather) writer, converted to the Parquet catalog once per closed hour. Kept apart from the trader so a disk problem never touches trading.

## Stack

- Python 3.14, `trading-core[node]` (NautilusTrader 1.231.0)
- Entry point: `recorder.main:main`; `recorder/actor.py` holds the `RecordingActor`

## Commands

```bash
docker compose build recorder
docker compose run --rm --no-deps -T recorder pytest -q
```

## Conventions

- Never an execution client, and never Kraken API keys in any environment (the env guard enforces this).
- Readers never touch the open hour of the catalog; a crash loses at most that hour.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
