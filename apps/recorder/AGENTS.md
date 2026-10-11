# recorder (apps/recorder)

## Overview

A separate Nautilus node with data clients only. It records L2 book deltas and trades for the BTC perp (`RECORDER_INSTRUMENTS`, BTC only since 2026-10-08, spec 0007) through Nautilus's streaming (feather) writer, converted to the Parquet catalog once per closed hour. Kept apart from the trader so a disk problem never touches trading.

## Stack

- Python 3.14, `trading-core[node]` (NautilusTrader 1.231.0)
- Entry point: `recorder.main:main`; `recorder/actor.py` holds the `RecordingActor`

## Commands

```bash
docker compose build recorder
docker compose run --rm --no-deps -T recorder pytest -q
```

## Conventions

- Never an execution client, and never Kraken API keys in any environment (the env guard enforces this). It mounts no secret files at all.
- Readers never touch the open hour of the catalog; a crash loses at most that hour.
- The compactor fails closed: a feather file that fails to read before its end raises `FeatherUnreadable` and fails the pass (only a torn tail at the end is accepted), and an unreadable holes line becomes a fail safe hole logged as `sequence_file_bad_lines`. Catalog files are written only through `trading_core.catalog.sink`'s `atomic_write` or `HourSink`, never a bare rename.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
