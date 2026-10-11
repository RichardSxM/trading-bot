# recorder (apps/recorder)

## Overview

A separate Nautilus node with data clients only. It records L2 book deltas and trades for the BTC perp (`RECORDER_INSTRUMENTS`, BTC only since 2026-10-08, spec 0007) through Nautilus's streaming (feather) writer, converted to the Parquet catalog once per closed hour by the `compactor` service. Kept apart from the trader so a disk problem never touches trading.

## Stack

- Python 3.14, `trading-core[node]` (NautilusTrader 1.231.0)
- Entry point: `recorder.main:main`; `recorder/actor.py` holds the `RecordingActor`
- The same image runs a second service, `compactor` (`python -m recorder.compactor.main`, under `nice -n 19`, `cpu_shares: 256`, `network_mode: none`, no secrets): every `COMPACTOR_INTERVAL_S` it turns closed feather hours into the hourly Parquet catalog, writes gaps and daily manifests, and deletes local hours only after the backup's cutoff (spec 0007)

## Key files

| File | Owns |
|---|---|
| `src/recorder/actor.py` | `RecordingActor` (subscriptions, the sequence watch on its 60 s timer) and `DiskGuardActor` |
| `src/recorder/sequence_holes.py` | `SequenceWatch` and `HolesFile`: writes `live/<run>/sequence_holes.jsonl` (AC-15); a failed write stops the recorder with exit 75 |
| `src/recorder/health.py`, `disk.py`, `live_files.py`, `settings.py` | The health check (heartbeat, a delta file under 60 s old, free space), the hard disk floor, and the current run rule; standard library only, so the 10 s health check never imports Nautilus or pyarrow |
| `src/recorder/queue_depth.py` | Data engine queue depth warnings (`recorder_queue_high`, AC-13) |
| `src/recorder/compactor/main.py` | The pass loop, holes reading, retention, `CompactorSettings` |
| `src/recorder/compactor/convert.py` | `read_chunks` and `HourStream`: feather to Parquet per hour, the reorder window, the carried book, `sequence` gaps |
| `src/recorder/compactor/store.py` | Catalog paths and `done.json`, the single commit point: an hour is done only once `done.json` lists it |
| `src/recorder/compactor/days.py`, `instruments.py` | Gap files and daily manifests (AC-4, AC-5); one instrument definition row per hour |
| `src/recorder/compactor/bridge.py` | The laptop backlog bridge gap (`--offline` mode too); kept but unused since the backlog was dropped (spec 0007 AC-9) |

## Commands

```bash
docker compose build recorder
docker compose run --rm --no-deps -T recorder pytest -q
docker compose logs -f compactor                  # pass results: compactor_pass_failed, catalog_hour_mismatch, book_gap
scripts/fetch-catalog.sh                          # laptop: pull catalog days from B2 (read only key)
```

## Conventions

- Never an execution client, and never Kraken API keys in any environment (the env guard enforces this). It mounts no secret files at all.
- Readers never touch the open hour of the catalog; a crash loses at most that hour.
- The compactor fails closed: a feather file that fails to read before its end raises `FeatherUnreadable` and fails the pass (only a torn tail at the end is accepted), and an unreadable holes line becomes a fail safe hole logged as `sequence_file_bad_lines`. Catalog files are written only through `trading_core.catalog.sink`'s `atomic_write` or `HourSink`, never a bare rename.
- Pure rules (gaps, hours, sequence, retention, queue depth) live in `trading_core.marketdata`; the shared hour writer and book carry in `trading_core.catalog`. Keep new logic there and the recorder files thin.
- Gotcha: `RecorderSettings.recorder_instruments` still defaults to BTC and ETH in code; Compose and `.env` set `PF_XBTUSD.KRAKEN` only. Set it explicitly whenever you run the recorder outside Compose.
- Reruns must give identical files: the reorder window state and gaps are stored in `done.json`, so a pass killed anywhere reruns to the same bytes (AC-11). Tests for any compactor change compare a rerun against one pass.

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
