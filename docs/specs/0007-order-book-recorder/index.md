# 0007. Record the order book as hourly Parquet with a book snapshot per hour, gaps logged, and a bounded disk

**Date**: 2026-10-05
**Status**: In Progress

## Summary

The recorder already captures the BTC and ETH perp order books, but only as raw files that grow without limit. On 2026-10-05 they filled the VPS disk. This spec turns the stream into compressed hourly Parquet files (a compact, column based format the backtester reads). Each hour starts with a full book snapshot, so a replay can begin at any hour. A new `compactor` service does the conversion. It also logs every gap, reports the size per day, and deletes old local hours only after they are safely in the B2 backup. The recorder becomes unhealthy, and stops, before it can fill the disk again.

## Requirements

**User stories**:
- As the trader's owner, I want the order book and trades recorded around the clock, so scalping ideas can be tested on weeks of real data.
- As the owner, I want every hole in the data found and logged, so a backtest never trusts a stretch of time that was not recorded.
- As the owner, I want a replay to start at any hour, so I can test one busy hour without replaying everything before it.
- As the owner, I want to know the size cost per day, and I never want the recorder to fill the disk or lose stored history.
- As the owner, I want to pull chosen days onto the laptop for heavy research.

**Acceptance criteria**:
- **AC-1**: The recorder's stream writes only `OrderBookDeltas` (the batch type the data engine publishes; its rows are `OrderBookDelta`), `TradeTick`, and `CryptoPerpetual`. Its feather files rotate after each UTC hour boundary (`RotationMode.SCHEDULED_DATES`, 1 h interval, `rotation_time` 00:00, UTC). The writer rotates a stream on its first write at or after the boundary, so a file can end with rows whose `ts_init` is just before the boundary, and later boundaries drift by that write's delay. No other type's file appears in a new run folder.
- **AC-2**: Once an hour H is ready (see *Lifecycle of one hour*), the compactor finishes it within 10 minutes on the VPS for a 2 GB hour. It writes one Parquet file per type and instrument that has rows at `data/<class_to_filename(cls)>/<urisafe_identifier(instrument)>/<_timestamps_to_filename(H, H + 1 h − 1 ns)>`, using zstd compression. The rows are ordered by `ts_init`. It writes through a `.tmp` name followed by a rename. Before deleting any feather file, it checks that the Parquet row count equals the source row count, plus the snapshot rows. A mismatch keeps the feather and logs ERROR `catalog_hour_mismatch`. It then deletes each closed feather file whose rows all fall in hours recorded in `done.json`.
- **AC-3**: Every converted `order_book_deltas` hour whose instrument has a valid carried book for H − 1 begins with that book as snapshot deltas, built the way the adapter's `_request_order_book_snapshot` builds them.
  - One `CLEAR`, then one `ADD` per price level, for all levels on both sides: bids best first, then asks best first.
  - `order_id` 0, `sequence` = the last applied delta's sequence, `ts_event = ts_init = H`, and flag `F_SNAPSHOT` on every row, with `F_SNAPSHOT | F_LAST` on the final one.
  - Prices and sizes keep the instrument's precision.
  - Test: one hour replays on its own through `ParquetDataCatalog` and a `BacktestEngine`. At the first `F_LAST` row after every 60 s of data, and at the hour's last row, its book equals the book from replaying the same raw feather from the run start. The comparison is the top 20 levels per side, with exact prices and sizes.
- **AC-4**: Three kinds of gap are detected per instrument. Each is written to `gaps/<YYYY-MM-DD>.parquet` (the UTC day of its `start_ns`) and logged as WARNING `book_gap` with `instrument_id`, `kind`, `start_ns`, `end_ns`, and `duration_s`:
  - `reconnect`: a feed `CLEAR` delta after the run's first one. The gap runs from the previous delta's `ts_init` to the `CLEAR`'s `ts_init`. Synthetic snapshot rows are never read by detection, because detection runs on the feather.
  - `recorder_down`: from the last row of one run to the first row of the next run (for that instrument). A run that ends in a truncated batch counts from its last complete row.
  - `silence`: two consecutive deltas of one instrument in one run that are `BOOK_SILENCE_GAP_S` (10 s) or more apart. A span already reported as `reconnect` is not reported again as `silence`.
- **AC-5**: A UTC day is closed when the compactor's stream has passed the day's end and every hour of the day is in `done.json` (a converted hour, or an empty hour lying wholly inside a `recorder_down` gap). The compactor then writes `manifests/<YYYY-MM-DD>.json`. For each instrument and type, it holds rows, Parquet bytes, and converted hours, taken from the per hour stats in `done.json`. It also holds gap seconds by kind (each gap clipped to the day) and the day's total bytes. It logs INFO `catalog_day_closed` with the same totals.
- **AC-6**: Retention runs after each conversion pass. While the Parquet catalog (`data/`) is over `CATALOG_CAP_GB` (30), or free space on its file system is under `CATALOG_FREE_FLOOR_GB` (8), the compactor deletes the oldest whole hour (all types and instruments of that hour together). It stops once both limits hold. It deletes only hours whose files all have an mtime before the `cutoff` in a valid backup state file. Valid means the JSON parses, `snapshot_id` is not empty, and `cutoff` is not after now. It logs INFO `catalog_retention_deleted` per hour. If it still needs space but nothing is eligible, it logs ERROR `catalog_retention_blocked` with the bytes still needed, at most once per pass. A missing or invalid state file makes nothing eligible, and an invalid one also logs ERROR `backup_state_invalid`.
- **AC-7**: The recorder container is healthy only while three things hold: the heartbeat file is under 30 s old, the newest `order_book_deltas` feather file in the current run was modified under 60 s ago, and free space is at least `CATALOG_HARD_FLOOR_GB` (2). The existing 90 s `start_period` covers the time before the first file exists.
  - While running, if free space falls below the hard floor, the recorder logs CRITICAL `catalog_disk_full` (with `free_bytes`), stops the node, and exits with code 75.
  - On start, while free space is below the floor, it does not build the node. It logs CRITICAL `catalog_disk_full` once, stays unhealthy, and checks every 30 s. Once space returns, it logs INFO `catalog_disk_ok` and starts. There is no crash loop.
- **AC-8**: The nightly backup no longer puts `/data/catalog` in the main snapshot. It always runs a separate catalog step, even when an earlier step failed. That step takes a snapshot tagged `kfs` and `catalog` of `data/`, `gaps/`, and `manifests/` only, excluding `live/`, `.compactor/`, and `*.tmp`. On success it writes `catalog_backup.json` (`snapshot_id`, `cutoff` = the UTC time this catalog step began, as ISO 8601) to the `backup-state` volume, through a temp file and rename. The restore drill's `forget` keeps every snapshot tagged `catalog` or `backlog` (`--keep-tag catalog --keep-tag backlog`). Nothing prunes those snapshots.
- **AC-9**: The backlog is the raw feather recorded before the compactor shipped. The disk filled on 2026-10-06, so on 2026-10-08 the nine runs then on the VPS (49.8 GB, 2026-10-03 to 2026-10-06) were deleted there, after a check that B2 snapshot `f0603109` holds every file at the same size. That snapshot carries the tag `backlog`, and any later emptied snapshot gets the same tag. The drill keeps every `backlog` snapshot (`--keep-tag backlog`). The backlog converts once on the laptop: `fetch-catalog.sh` restores those runs into `./catalog/live/`, the compactor converts them there, and the resulting `data/`, `gaps/`, and `manifests/` files are copied into the VPS catalog, so the next catalog snapshot keeps them forever. The laptop conversion also writes one `recorder_down` gap from the backlog's last row to the first VPS run's first row. Once that catalog snapshot exists, the `backlog` tag is removed. On the VPS, dead runs (from restarts) are converted by the normal loop, oldest first. Before it starts on a dead run, the compactor needs free space of at least the run's feather bytes × `COMPACTOR_PARQUET_RATIO`, plus `CATALOG_FREE_FLOOR_GB`. If that is missing, it logs ERROR `backlog_needs_space` (with the bytes needed) once per pass and leaves the run untouched, while it keeps converting the live run. The 25 GB single file in the backlog is read batch by batch in one pass, and the compactor stays under its 512 MB memory limit. Each restart between runs becomes a `recorder_down` gap.
- **AC-10**: `scripts/fetch-catalog.sh <from-date> <to-date>` (both inclusive, UTC days) runs on the laptop. For each day it finds the newest `catalog` snapshot holding that day's files (`restic find --tag catalog`). It restores that day's `data/` hours, `gaps/<day>.parquet`, and `manifests/<day>.json` into `./catalog`. It runs restic in the `kfs/backup` image, with the repository from `RESTIC_REPOSITORY` in `.env`, and with a read only B2 key and the restic password from `./secrets`. A `ParquetDataCatalog("./catalog")` query returns the rows, and the instrument definition is present.
- **AC-11**: A conversion pass that is killed at any point, then run again, ends with the same set of Parquet files and gap rows, byte for byte in content. It leaves no `.tmp` file, no duplicate rows, and no deleted feather file whose hour is not in `done.json`. A book state is never applied twice.
- **AC-12**: If the source rows of one hour go backwards in `ts_init` (two runs writing at once), the compactor writes nothing for that hour, keeps its feather files, and logs ERROR `catalog_hour_overlap` with the hour and the files. It then marks every instrument's carried book invalid. Later hours still convert, but without a synthetic snapshot until each instrument's next feed `CLEAR`.

## Decision

**Chosen option**: Option 2: hourly feather rotation plus a separate compactor service (see [rationale.md](rationale.md)).

The recorder keeps streaming feather, now filtered and rotated hourly. A new `compactor` Compose service turns each closed hour into zstd Parquet in the Nautilus catalog layout, with a book snapshot at its start. It writes gaps and a daily manifest, then deletes old local hours only once the B2 backup holds them.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `docker-compose-skill` (`scientiacapital/skills`, `.claude/skills/docker-compose-skill/`)

## Feature design

### Pipeline

```
Kraken WS ──► recorder (Nautilus node)
              StreamingConfig: include_types, hourly rotation
              writes live/<run>/<type>/<instrument>/<...>.feather
                        │  (closed files only)
                        ▼
              compactor (loop every 60 s)
              1. plan ready hours        (core: pure)
              2. convert hour by hour    (pyarrow batches + Nautilus OrderBook carry)
                 └─ data/…parquet, gaps/<date>.parquet, .compactor/state
              3. delete converted feather
              4. day closed? manifests/<date>.json
              5. retention               (core: pure, reads backup-state)
                        │
                        ▼
              backup (nightly 02:30) ──► restic snapshot tag catalog ──► B2
                                     └─► backup-state/catalog_backup.json (cutoff)
```

### Catalog layout (on the `catalog` volume, `/srv/kfs/catalog` on the VPS)

| Path | Written by | Contents | Backed up |
|---|---|---|---|
| `live/<run uuid>/...feather` | recorder | the raw stream, hourly files | no |
| `data/order_book_deltas/<instrument>/<interval>.parquet` (`class_to_filename(OrderBookDelta)`) | compactor | one hour of deltas, starting with the snapshot | yes |
| `data/trade_tick/<instrument>/<interval>.parquet` | compactor | one hour of trades | yes |
| `data/crypto_perpetual/<instrument>/<interval>.parquet` | compactor | one row per hour: the newest definition seen up to the end of that hour (carried forward when the hour has none), so any single hour or day is self contained | yes |
| `gaps/<YYYY-MM-DD>.parquet` | compactor | gap rows, keyed by the gap's `start_ns` day | yes |
| `manifests/<YYYY-MM-DD>.json` | compactor | the day's size and gap totals | yes |
| `.compactor/books/<instrument>/<H>.parquet` | compactor | the carried book at the end of hour H (the two newest hours kept) | no |
| `.compactor/done.json` | compactor | the commit record (below) | no |

**`done.json`** (the single commit point, rewritten through a temp file and rename) holds:
- `hours`: each finished hour, with per stream rows and bytes.
- `carry`: for each instrument, the last hour whose book file is valid, or `null` when the book is invalid.
- `streams`: for each instrument, the last `ts_init` and run seen, so gap detection resumes across passes.
- `days`: the closed days.

An hour counts as done only once its entry is in `done.json`. Parquet files and a book file for an hour that is not yet listed are leftovers of a crash, and a rerun overwrites them.
| `.kfs-volume` | bootstrap | the disk marker (spec 0005) | no |

`<interval>` is `_timestamps_to_filename(H, H + 3_599_999_999_999)` from `nautilus_trader.persistence.catalog.parquet`. Hours are disjoint and contiguous, so the catalog's disjoint interval check always passes.

**Gap row** (Parquet schema): `instrument_id string`, `kind string` (`reconnect` | `recorder_down` | `silence`), `start_ns uint64`, `end_ns uint64`. Unique on `(instrument_id, kind, start_ns)`, and sorted by `start_ns`. Each new gap is merged into its day file (read, union, deduplicate, sort, write atomically), so a rerun gives the same rows (AC-11). A gap that crosses midnight stays whole in its start day's file. Manifests clip its seconds to each day.

**Manifest** (JSON): `{"date": "YYYY-MM-DD", "total_bytes": int, "streams": [{"instrument_id", "type", "rows", "bytes", "hours"}], "gaps": [{"instrument_id", "kind", "seconds"}]}`.

**Backup state** (`/state/catalog_backup.json` on the new `backup-state` volume): `{"snapshot_id": str, "cutoff": "YYYY-MM-DDTHH:MM:SSZ"}`.

### Lifecycle of one hour

`open` (the recorder writes it) → `closed` → `ready` → `converted` (Parquet renamed in place) → `done` (listed in `done.json`) → `feather deleted` → `backed up` (file mtime < cutoff) → `retired` (deleted by retention). A failure at any step leaves the hour at its previous state, and the next pass retries it.

- **Run**: a folder `live/<uuid>/` written by one recorder process. Runs are ordered by the earliest `ts_init` across their files. The **current run** is the run folder with the newest creation time (`st_ctime` of the folder), and there is only one recorder.
- **Closed file**: a feather file is closed when its run is not the current run (the process that wrote it is gone), or when the same run folder has a newer file for the same type and instrument (it rotated). The current run's newest file per stream is never closed, however quiet it is. File names are the Nautilus stream names (`order_book_deltas/`, `trade_tick/`, and `crypto_perpetual_<ns>.feather`), which differ from the catalog's `class_to_filename` names.
- **Ready hour**: hour H is ready when now ≥ H + 1 h + `COMPACTOR_GRACE_S`, and the compactor's stream has passed H + 1 h: it has read a closed file with a row at or after H + 1 h, for every delta and trade stream that had rows in H. Once its run has ended, the last hour of a dead run is ready too. The instrument stream does not gate readiness. It holds the whole venue's list (about 226 rows), refreshed every 60 minutes from the recorder's start, so its file rotates up to an hour late.
- **Instrument file**: the one exception to the closed file rule. The compactor reads the current run's `crypto_perpetual_<ns>.feather` read only, up to its last complete batch, so the first hours of a run have a definition. It never deletes or commits that file while it is open.

**One pass, in time order.** The compactor reads closed feather files in order of (run order, then file start) and never scans a file twice. A small index in `done.json` records, per finished file, how far it was read. Rows go into the open hour's writers. When the first row at or after H + 1 h arrives, it finishes hour H: snapshot, verify the counts, rename, save the book, then commit to `done.json`. So the 25 GB file is read once, from start to end, and becomes about 45 hours.

### Conversion rules

- **Reading**: `pyarrow.ipc` record batches, one at a time. The stream holds about 1.5 rows per batch, so batches are gathered into chunks of at most 8,192 rows before each flush to the Parquet writer and the book (Milestone 0: 65,536 row chunks were killed at 512 MB; 8,192 held 343 MB on a 2 GB file). A truncated final message (a killed recorder) ends the file at its last complete batch, and the rest lies inside the next `recorder_down` gap.
- **Hour assignment**: by `ts_init`. Rows of one feed message (up to its `F_LAST`) share a `ts_init`, so a group never splits across hours.
- **Order**: rows are appended in stream order. If a row's `ts_init` is below the last written one, AC-12 applies. Nothing is sorted in memory.
- **Book carry**: per instrument, a `nautilus_pyo3.OrderBook(L2_MBP)` is restored (`ArrowSerializer.deserialize` returns pyo3 deltas) from `.compactor/books/<instrument>/<H−1>.parquet` when `carry` in `done.json` says H − 1 is valid. Every delta of the hour is applied to it, and the result is saved as `<H>.parquet` before the commit.
- **Snapshot at hour start** (AC-3): written when the carry is valid for H − 1, which means no gap and no invalid mark between the end of H − 1 and H. Otherwise the hour gets no synthetic snapshot. That instrument's rows before its next feed `CLEAR` are still written, because they are real data and sit inside the gap the detector reports. They are not applied to a book. The carry becomes valid again from that `CLEAR` on.
- **First hour ever**: there is no carry, so the hour starts without a synthetic snapshot. The run's first feed `CLEAR` (checked in Milestone 0) starts the carry.
- **Trades**: copied as they are, with no snapshot.
- **Instruments**: one `CryptoPerpetual` row per hour (see the layout table).
- **Verification before delete** (AC-2): rows in each Parquet = source rows of the hour + snapshot rows, checked by reading the Parquet footer.
- **Schema**: the Parquet table keeps the feather stream's Arrow schema and its metadata, which are what Nautilus's catalog reader decodes (Milestone 0 proves this). Test: `catalog.order_book_deltas(instrument_ids=[...], start=H, end=H+1h)` returns the rows.
- **Memory and CPU**: one batch plus the carried books are resident. The service has `mem_limit: 512m`, `cpus: 1.0`, and runs at `nice 10`, so the trader always wins CPU.

### Value sourcing

| Action | Value produced | Source |
|---|---|---|
| plan | closed files, read order | listing of `live/`, run folder `st_ctime` (the current run), sibling files per stream, the run order (earliest `ts_init`, kept in `done.json` once read), the read offsets in `done.json` |
| plan | hour ready | `now`, `COMPACTOR_GRACE_S`, and the stream position (the first row at or after H + 1 h, or the end of a dead run) |
| convert | Parquet file path | `class_to_filename`, `urisafe_identifier`, `_timestamps_to_filename(H, H + 1 h − 1 ns)` (Nautilus 1.231.0) |
| convert | snapshot deltas | `.compactor/books/<instrument>/<H−1>.parquet` when `carry` in `done.json` is valid for H − 1; `sequence` from the last applied delta; `ts = H` |
| convert | instrument row | the newest `CryptoPerpetual` row for that instrument read up to H + 1 h (closed files, or the current instrument file up to its last complete batch), carried in memory and in `done.json` |
| verify | expected rows | the count of source rows routed to the hour, plus the snapshot rows written |
| detect gaps | gap start and end | `ts_init` of the neighbouring rows, the last `ts_init` and run per instrument in `done.json` (`streams`), `BOOK_SILENCE_GAP_S` |
| backlog check | bytes needed | the dead run's feather bytes × `COMPACTOR_PARQUET_RATIO` + `CATALOG_FREE_FLOOR_GB` |
| manifest | bytes, rows, hours | per hour stats in `done.json` (`hours`) |
| manifest | gap seconds | `gaps/<date>.parquet` and the previous day's file (for a gap that crosses midnight), each clipped to the day |
| retention | catalog bytes | sum of file sizes under `data/` |
| retention | free bytes | `os.statvfs(catalog_path)` |
| retention | backed up or not | `cutoff` in a valid `/state/catalog_backup.json` compared with each file's mtime |
| recorder health | stream freshness | mtime of the newest file under `live/<current run>/order_book_deltas/` (current run = the newest run folder by `st_ctime`) |
| recorder health and floor | free bytes | `os.statvfs(catalog_path)` |
| backup state | cutoff | `date -u` taken at the start of the catalog step in `docker/backup/run.sh` |
| fetch script | repository and keys | `RESTIC_REPOSITORY` in `.env`, `secrets/restic_password`, `secrets/b2_read_key_id`, `secrets/b2_read_key` |
| fetch script | which snapshot per day | the newest snapshot tagged `catalog` that `restic find --tag catalog` reports for a path of that day |
| fetch script | which files per day | the 24 hour intervals of the day under `data/`, plus `gaps/<day>.parquet` and `manifests/<day>.json` |

### Code placement

- `packages/core/src/trading_core/marketdata/` (pure, no I/O, docstrings, unit tests in core):
  - `hours.py`: the hour math (hour of a `ts_init`, hour bounds, the interval for a file name), the ready rule (now, grace, stream position), the day close rule, and the backlog space check.
  - `gaps.py`: given an ordered stream of `(ts_init, is_clear, run)` per instrument and the state carried from earlier passes, it returns the gap records and the new state. It streams across batches, and handles the dedupe of reconnect versus silence.
  - `retention.py`: given the hours with their bytes and newest mtime, the cutoff, catalog bytes, free bytes, cap, and floor, it returns the hours to delete and the bytes still needed when it is blocked. It also validates a parsed backup state.
  - Typed exceptions: `HourOverlapError`, `BackupStateInvalid`, `HourCountMismatch`.
- `apps/recorder/src/recorder/compactor/` (adapter): `main.py` (loop, settings, logging), `convert.py` (pyarrow and OrderBook), `store.py` (paths, atomic writes, `done.json`, the backup state reader).
- `apps/recorder/src/recorder/`: `main.py` (`StreamingConfig`, and the wait at start while below the floor), `disk.py` (the free space floor, next to `volume.py`), `health.py` (the container health check), and a `DiskGuardActor` that checks free space every 10 s. Below the floor it logs CRITICAL, flushes the logs, and stops the process with exit code 75 (by sending itself `SIGTERM`, so the node shuts down cleanly, with `os._exit(75)` after 20 s as a fallback). The restart then lands in the wait at start.

### Compose and backup changes

- `compose.yaml`, new service `compactor`. It uses the recorder image and runs `python -m recorder.compactor.main`. Environment: base env, `PROCESS_NAME: compactor`, `CATALOG_PATH`, `BACKUP_STATE_PATH: /state/catalog_backup.json`, and the settings below. Volumes: `catalog:/data/catalog` (read and write), `backup-state:/state:ro`. No network access is needed; it gets `network_mode: none`. `mem_limit: 512m`, `cpus: 1.0`, and the command runs under `nice -n 10`. It has no secrets. Health: a heartbeat file touched by the loop, under 180 s old.
- `compose.vps.yaml`: the compactor gets the same catalog bind as the recorder and `CATALOG_REQUIRE_MARKER: "true"` (it uses the same marker check before starting).
- The recorder's health check becomes `python -m recorder.health`.
- The backup service mounts `backup-state:/state` read and write.
- `docker/backup/run.sh`: `step_upload` drops `/data/catalog`. `run_backup` runs a new `step_catalog` after the main chain, whether the chain passed or failed (AC-8). A catalog step failure logs ERROR `backup_failed` with `step=catalog`, the same as the other steps. The health marker (`LAST_SUCCESS`) still means that the main chain passed.
- `scripts/restore-drill.sh`: `RETENTION` gains `--keep-tag catalog` (`--keep-tag backlog` was added on 2026-10-08, AC-9). Snapshots carrying that tag are never forgotten, so the drill's `MAX_FORGET` and recent snapshot guards only ever see the main snapshots.

### Key invariants

- `done.json` is the only commit point. The order for each hour is: Parquet renamed, counts verified, book file saved, then `done.json` rewritten. A feather file is deleted only after every hour it holds is in `done.json`.
- A book state is applied only from the `<H−1>` file that `done.json` marks valid, so a rerun never applies an hour twice.
- Retention never deletes a file whose mtime is at or after the backup cutoff. With no valid state file, it deletes nothing.
- Parquet files are written once per hour and never appended to. A rerun replaces them with identical content.
- Within each Parquet file, `ts_init` never decreases.
- The compactor never touches a file that is not closed, and never reads a file twice. The one exception is the current run's instrument file, which it reads read only and never deletes while open.
- The recorder has no execution client and no secrets (unchanged). The compactor has no network and no secrets.

### Security model

All data here is public market data. The compactor gets no secrets and no network. The backup keeps its existing B2 key (no delete right). The laptop fetch uses a new B2 application key limited to `listBuckets`, `listFiles`, `readFiles` on this bucket. It is stored as `secrets/b2_read_key_id` and `secrets/b2_read_key` (gitignored, never committed). The restic password file is `secrets/restic_password`. You create the key in the B2 console; it never goes to the VPS.

### Configuration required

Through `trading_core.settings`, validated at start:
- `CATALOG_CAP_GB` (compactor, default 30): the cap on `data/` size.
- `CATALOG_FREE_FLOOR_GB` (compactor, default 8): the free space retention keeps.
- `CATALOG_HARD_FLOOR_GB` (recorder, default 2): below this, the recorder stops, or waits at start. It must be below `CATALOG_FREE_FLOOR_GB`. Each process validates the pair at start, because both settings are shared through `base-env`.
- `COMPACTOR_PARQUET_RATIO` (compactor, default set from the Milestone 1 measurement, as a Decimal): Parquet bytes per feather byte, used by the backlog space check.
- `BOOK_SILENCE_GAP_S` (compactor, default 10): the silence gap threshold.
- `COMPACTOR_GRACE_S` (compactor, default 300): the wait after an hour ends before it counts as ready.
- `COMPACTOR_INTERVAL_S` (compactor, default 60): the time between passes.
- `BACKUP_STATE_PATH` (compactor, default `/state/catalog_backup.json`).

### Logs

| Code | Level | Process | When |
|---|---|---|---|
| `catalog_hour_converted` | INFO | compactor | each hour written, with rows and bytes per stream and the duration |
| `book_gap` | WARNING | compactor | each gap (AC-4) |
| `catalog_day_closed` | INFO | compactor | each manifest (AC-5) |
| `catalog_retention_deleted` | INFO | compactor | each hour retired |
| `catalog_retention_blocked` | ERROR | compactor | space needed but nothing is backed up |
| `catalog_hour_overlap` | ERROR | compactor | AC-12 |
| `catalog_hour_mismatch` | ERROR | compactor | Parquet rows do not match the source (AC-2) |
| `backlog_needs_space` | ERROR | compactor | a dead run cannot convert yet (AC-9) |
| `backup_state_invalid` | ERROR | compactor | the backup state file is unreadable or wrong (AC-6) |
| `compactor_pass_failed` | ERROR | compactor | any other exception in a pass, with the hour; the loop continues |
| `catalog_disk_full` | CRITICAL | recorder | AC-7 |
| `catalog_disk_ok` | INFO | recorder | space returned after a wait (AC-7) |

Feature 12 alerts on the ERROR and CRITICAL codes, and on `book_gap` with a duration over 60 s.

### Critical test scenarios

- Happy path: a fixture run with two hours of deltas and trades converts to two Parquet hours, the feather files are deleted, and a lone replay of hour two matches the full replay. Verifies **AC-2**, **AC-3**.
- Restart: two runs with a 40 s hole and a mid run `CLEAR` produce one `recorder_down` gap and one `reconnect` gap, and the hour after the restart starts at the feed snapshot. Verifies **AC-4**, **AC-3**.
- Silence: 12 s without a delta for ETH gives one `silence` gap, and 9 s gives none. Verifies **AC-4**.
- Crash: kill the pass at each commit step in turn (after the rename, after the book save, after `done.json`, during the feather delete), then rerun. The files and gap rows are identical, with no `.tmp`, no duplicates, and no book applied twice. Verifies **AC-11**.
- Overlap: two runs whose rows interleave in one hour log `catalog_hour_overlap`, the feather is kept, and the next hour has no synthetic snapshot until the feed `CLEAR`. Verifies **AC-12**.
- Mismatch: a forced row count difference keeps the feather and logs `catalog_hour_mismatch`. Verifies **AC-2**.
- Quiet live file: the current run's trade file unchanged for 20 minutes is never treated as closed. Verifies **AC-2**.
- Truncated tail: a fixture file cut in the middle of a batch converts up to its last complete batch. Verifies **AC-4**.
- Midnight: a `recorder_down` gap from 23:50 to 00:20 lies in the first day's gap file, and each manifest counts its own part. A day with two empty hours inside a gap still closes. Verifies **AC-5**.
- Retention: with the cap exceeded and the cutoff before the oldest hour, nothing is deleted and `catalog_retention_blocked` is logged. With the cutoff moved forward, the oldest hours go first, whole, until both limits hold. A cutoff in the future logs `backup_state_invalid`. Verifies **AC-6**.
- Disk floor: a fake `statvfs` below 2 GB makes the guard log `catalog_disk_full` and exit 75. At start, it waits and then logs `catalog_disk_ok` once space returns. The health check fails on a stale stream file. Verifies **AC-7**.
- Types: a new run folder contains only the three types, rotated at the hour. Verifies **AC-1**.
- Memory and backlog: converting a 2 GB synthetic file inside the 512 MB container succeeds and reads the file once. With too little free space, the run is left alone and `backlog_needs_space` is logged. Verifies **AC-9**.
- Fetch: on a local restic repository holding two days in different snapshots, a two day fetch restores both, and the instrument query works. Verifies **AC-10**.
- Backup: a run against a local restic repository creates the `catalog` tagged snapshot without `live/` and writes the state file. Verifies **AC-8**.

## Build plan

Tracer Bullet: first one real hour of BTC deltas goes all the way from the stream to a replayable Parquet file, then each segment thickens.

**Milestone 0: gate (a throwaway spike before any build work)**

This milestone proves five assumptions on Nautilus 1.231.0, on the laptop, with a brief run and then the stack down. If any of them fails, the build stops and the feature goes back to `/architect`, with no workaround.

1. The five checks:
   - `StreamingConfig` with `include_types` and `SCHEDULED_DATES` at a 1 h interval writes only those types and starts a new file per stream at each UTC hour.
   - A Parquet file written with pyarrow from feather batches (same schema and metadata) is read back by `ParquetDataCatalog.order_book_deltas` and replays in a `BacktestEngine`.
   - The Kraken feed's first deltas after a subscription are a `CLEAR` with `F_SNAPSHOT`, and a reconnect produces another one.
   - Applying one recorded hour of BTC deltas to an `OrderBook` in the compactor's way takes under 5 minutes on one VPS class core.
   - The 512 MB memory limit holds while streaming a 2 GB feather file.

   Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-9** (as preconditions).

   **Result (2026-10-08): all five passed.**
   - Types and rotation: only the three types were written, and every stream started a new file at the boundary (deltas within 15 ms, trades up to 3 s late). The findings on `OrderBookDeltas`, drift, and the instrument stream are folded into AC-1 and the ready rule.
   - Catalog round trip: the catalog read all 20,809 rows of a pyarrow written zstd file, and a `BacktestEngine` built the same book as a direct replay.
   - Feed snapshots: the first deltas after subscribing, and the first after an 82 s network cut, are a `CLEAR` with `F_SNAPSHOT`.
   - Speed: 15.6 minutes of VPS BTC deltas (523,008 rows) converted with the book carried in 44.6 s on a busy VPS core at `nice 19`, about 2.9 minutes per hour.
   - Memory: a 2.2 GB file streamed at a hard 512 MB limit with 343 MB peak RSS, using 8,192 row chunks.

**Milestone 1: thin thread (one hour, BTC deltas, replayable)**
1. Recorder `StreamingConfig`: `include_types` and hourly scheduled rotation. Satisfies **AC-1**.
2. Core `marketdata/hours.py` with unit tests. A minimal compactor converts ready hours of `order_book_deltas` for one instrument, with book carry and the snapshot at hour start, written atomically. Satisfies **AC-2**, **AC-3**.
3. The replay equivalence test. Satisfies **AC-3**.
4. Measure on the laptop for at least 2 hours: Parquet bytes per hour against feather bytes per hour, and conversion time. Write the measured ratio and the expected GB per day into this spec's Consequences, and set the `COMPACTOR_PARQUET_RATIO` default from it, with a 1.5× safety margin. (Brief laptop run, then stack down.)

**Milestone 2: thicken conversion**
5. All instruments and types, multi source hours, the overlap check, feather deletion, `done.json`, and idempotent reruns. Satisfies **AC-2**, **AC-11**, **AC-12**.
6. The compactor Compose service (512 MB, no network), the heartbeat health check, and the dead run path with the space check and the memory test. Satisfies **AC-9**.

**Milestone 3: gaps and size per day**
7. Core `marketdata/gaps.py`, the gap file, and the `book_gap` logs. Satisfies **AC-4**.
8. The daily manifest and `catalog_day_closed`. Satisfies **AC-5**.

**Milestone 4: disk safety and backup**
9. Recorder `disk.py`, `DiskGuardActor`, the floor check at start, and `recorder.health`. Satisfies **AC-7**.
10. Backup `step_catalog`, the `backup-state` volume, and `--keep-tag catalog` in the drill. Satisfies **AC-8**.
11. Core `marketdata/retention.py` and the compactor retention step. Satisfies **AC-6**.

**Milestone 5: laptop and VPS**
12. `scripts/fetch-catalog.sh` and the read only B2 key files. Satisfies **AC-10**.
13. Deploy to the VPS. On the laptop, fetch the `backlog` runs from B2, convert them, copy the results into the VPS catalog, and remove the `backlog` tag after the next catalog snapshot. Confirm that `df -h /srv/kfs` stays flat over 24 h and that a manual backup writes the state file. Satisfies **AC-9**, **AC-6**, **AC-8**.

## Migration plan

**Strategy**: in place, one deploy. The old run folders are just dead runs to the new compactor.
**Phases**:
1. Deploy the new recorder and compactor. The recorder starts a new run folder with hourly files. The compactor converts the two old folders in the background, oldest first.
2. The first nightly backup after the deploy uploads `data/` and writes the state file. From then on, retention can work.
**Rollback**: redeploy the previous sha. The old recorder ignores `data/` and keeps streaming into `live/`. Stop the compactor service. Parquet already written stays valid.
**Risks**:
- **CPU.** The backlog conversion competes for CPU with the trader on a 4 GB box. It is capped at one core and runs at `nice 10`. Progress shows in `catalog_hour_converted`.
- **Disk.** Until the compactor ships, the recorder refills the 49 GB disk in about 1.5 days. Each time, after a nightly backup, the runs are checked against B2 by name and size, that snapshot is tagged `backlog`, and the runs are deleted (AC-9). A dead run on the VPS frees nothing until it is read to its end, while its Parquet grows next to it and the live run keeps writing. Retention cannot free anything before the first catalog backup. The backlog space check (AC-9) refuses to start a dead run without room. If it logs `backlog_needs_space`, you decide: free space, offload the run, or delete it.
- **Before the deploy.** Check `df -h /srv/kfs`. The live run alone needs about a day of feather plus the 8 GB floor until the first nightly catalog backup.

## Consequences

**Positive**:
- The disk stays bounded. The recorder can no longer report healthy while it writes nothing, and it cannot fill the disk past the hard floor.
- Any hour replays on its own, and the gap file tells the simulator which stretches to skip.
- The size per day becomes a measured number in every manifest, not a guess.
- B2 keeps the full history for good, at the cost of the unique compressed bytes.

**Negative / tradeoffs**:
- One more service to run and watch (the compactor), and a second restic snapshot each night.
- Data reaches Parquet only an hour or so after it happens. Live strategies do not read the catalog, so only research waits.
- When backups stop, retention stops too, and after about a day the recorder halts at the hard floor. That is the chosen behaviour (stop recording and keep the data). It still loses new data until someone acts.
- The synthetic snapshot depends on the carried book being right. A bug there would quietly corrupt every hour's starting book, which is why the replay equivalence test is part of the build. The raw feather is gone after conversion, so such a bug cannot be fixed by converting again. The hours stay replayable from their own start, but not from a synthetic snapshot.
- After a gap, the hour has no synthetic snapshot. A replay that starts there has no book until the feed's next `CLEAR`.
- The catalog's file names and Arrow schema come from Nautilus internals (`_timestamps_to_filename`). An upgrade from 1.231.0 must run the replay test again.

**Neutral**:
- Measured before this spec: 0.75 GB/h on average and about 2 GB/h at peak of raw feather, 99% book deltas. Milestone 0 saw zstd Parquet at about 3% of the feather bytes (an early sample, roughly 1 to 1.5 GB per day). Milestone 1 measures it over 2 hours and writes it here.
- B2 cost grows linearly with compressed bytes; the measured GB per day sets it.
- The backup no longer uploads raw feather, so the nightly upload gets smaller.

## Follow-up

- [ ] Milestone 1 writes the measured Parquet GB per day and the compression ratio into Consequences, and updates spec 0001's follow-up "measure the recorder's compressed bytes per day".
- [ ] Feature 12: alert on `catalog_disk_full`, `catalog_retention_blocked`, `catalog_hour_overlap`, `compactor_pass_failed`, `backup_failed step=catalog`, and long `book_gap`.
- [ ] Feature 15 (simulator): read `gaps/` and skip or split replays at gaps.
- [ ] Feature 20 (market context): add its types to `include_types` and to the compactor's type list.
- [ ] Spec 0005: after this ships, run the soak sequence (kfs-soak smoke run, deploy during an open position, 72 h soak).
- [ ] Revisit the 30 GB cap once the measured size per day is known: aim for at least 7 days of local hours.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
