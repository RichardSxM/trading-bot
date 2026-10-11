# 0007. Record the order book as hourly Parquet with a book snapshot per hour, gaps logged, and a bounded disk

**Date**: 2026-10-05
**Status**: In Progress

## Summary

The recorder already captures the BTC perp order book (BTC and ETH until 2026-10-08), but only as raw files that grow without limit. On 2026-10-05 they filled the VPS disk. This spec turns the stream into compressed hourly Parquet files (a compact, column based format the backtester reads). Each hour starts with a full book snapshot, so a replay can begin at any hour. A new `compactor` service does the conversion. It also logs every gap, reports the size per day, and deletes old local hours only after they are safely in the B2 backup. The recorder becomes unhealthy, and stops, before it can fill the disk again. When the recorder falls behind and Nautilus delivers rows out of order, the compactor puts them back in order within a 10 minute window. Anything later than that is kept apart and backed up, never silently dropped (decided 2026-10-08). On 2026-10-08 the scope narrowed: the 2 CPU VPS could not keep up with BTC and ETH at 3.2 GB/h, so it records the BTC perp only, and backtests use the free Tardis days (the first day of each month, `PF_XBTUSD` since 2022-03-22) rather than our own history. The old backlog is dropped, not converted. Also on 2026-10-08, `/check verify` found that the recorder had silently lost runs of book messages (up to 189k in one go) while it was overloaded, and the book after each hole went wrong until the next reconnect. So the recorder now watches the adapter's message counter and records every hole. The compactor turns each hole into a `sequence` gap and stops trusting the book until the next feed `CLEAR`. A resubscribe to fetch a fresh book sooner was tried and dropped: it brought no snapshot.

## Requirements

**User stories**:
- As the trader's owner, I want the order book and trades recorded around the clock, so scalping ideas can be tested on weeks of real data.
- As the owner, I want every hole in the data found and logged, so a backtest never trusts a stretch of time that was not recorded.
- As the owner, I want a replay to start at any hour, so I can test one busy hour without replaying everything before it.
- As the owner, I want to know the size cost per day, and I never want the recorder to fill the disk or lose stored history.
- As the owner, I want to pull chosen days onto the laptop for heavy research.

**Acceptance criteria**:
- **AC-1**: The recorder's stream writes only `OrderBookDeltas` (the batch type the data engine publishes; its rows are `OrderBookDelta`), `TradeTick`, and `CryptoPerpetual`. Its feather files rotate after each UTC hour boundary (`RotationMode.SCHEDULED_DATES`, 1 h interval, `rotation_time` 00:00, UTC). The writer rotates a stream on its first write at or after the boundary, so a file can end with rows whose `ts_init` is just before the boundary, and later boundaries drift by that write's delay. No other type's file appears in a new run folder.
- **AC-2**: Once an hour H is ready (see *Lifecycle of one hour*), the compactor finishes it within 10 minutes on the VPS for a 2 GB hour. It writes one Parquet file per type and instrument that has rows at `data/<class_to_filename(cls)>/<urisafe_identifier(instrument)>/<_timestamps_to_filename(H, H + 1 h − 1 ns)>`, using zstd compression. The rows are ordered by `ts_init`. It writes through a `.tmp` name followed by a rename. Before deleting any feather file, it checks that the Parquet row count equals the source rows routed to the hour, minus the hour's late rows (AC-12), plus the snapshot rows. A mismatch keeps the feather and logs ERROR `catalog_hour_mismatch`. It then deletes each closed feather file whose rows all fall in hours recorded in `done.json`.
- **AC-3**: Every converted `order_book_deltas` hour whose instrument has a valid carried book for H − 1 begins with that book as snapshot deltas, built the way the adapter's `_request_order_book_snapshot` builds them.
  - One `CLEAR`, then one `ADD` per price level, for all levels on both sides: bids best first, then asks best first.
  - `order_id` 0, `sequence` = the last applied delta's sequence, `ts_event = ts_init = H`, and flag `F_SNAPSHOT` on every row, with `F_SNAPSHOT | F_LAST` on the final one.
  - Prices and sizes keep the instrument's precision.
  - Test: one hour replays on its own through `ParquetDataCatalog` and a `BacktestEngine`. At the first row with `ts_init` at or after H + k × 60 s (not the first `F_LAST` row: Kraken feed rows carry flags 0, measured 2026-10-08 on 2,980,636 rows), and at the hour's last row, its book equals the book from replaying the same raw feather from the run start. The comparison is the top 20 levels per side, with exact prices and sizes.
- **AC-4**: Four kinds of gap are detected per instrument. Each is written to `gaps/<YYYY-MM-DD>.parquet` (the UTC day of its `start_ns`) and logged as WARNING `book_gap` with `instrument_id`, `kind`, `start_ns`, `end_ns`, and `duration_s`. Detection reads the rows in the order the reorder window (AC-12) writes them, and never reads late rows:
  - `reconnect`: a feed `CLEAR` delta after the run's first one. The gap runs from the previous delta's `ts_init` to the `CLEAR`'s `ts_init`. Synthetic snapshot rows are never read by detection, because detection runs on the feather.
  - `recorder_down`: from the last row of one run to the first row of the next run (for that instrument). A run that ends in a truncated batch counts from its last complete row.
  - `silence`: two consecutive deltas of one instrument in one run that are `BOOK_SILENCE_GAP_S` (10 s) or more apart. A span already reported as `reconnect` or `sequence` is not reported again as `silence`.
  - `sequence`: a hole in the run's message counter, from the recorder's holes file (AC-15, AC-16). It runs from the hole's `before_ns` to that instrument's first feed `CLEAR` at or after the hole's `after_ns`, or to the instrument's last row of the run when no `CLEAR` comes (AC-16 has the full rule). The `CLEAR` that ends a `sequence` gap is not also a `reconnect` gap. The `book_gap` log adds `missing` (the count of lost messages, summed when several holes fall in one gap).
- **AC-5**: A UTC day is closed when the compactor's stream has passed the day's end and every hour of the day is in `done.json` (a converted hour, or an empty hour lying wholly inside a `recorder_down` gap) for each instrument in `RECORDER_INSTRUMENTS` (the compactor gets the recorder's list); an instrument dropped from the list no longer holds a day back, and its hours still appear in the manifest. The compactor then writes `manifests/<YYYY-MM-DD>.json`. For each instrument and type, it holds rows, Parquet bytes, converted hours, late rows, and incomplete hours (hours with late rows, AC-12), taken from the per hour stats and `incomplete` in `done.json`. It also holds gap seconds by kind (each gap clipped to the day) and the day's total bytes. It logs INFO `catalog_day_closed` with the same totals.
- **AC-6**: Retention runs after each conversion pass. While the Parquet catalog (`data/`) is over `CATALOG_CAP_GB` (30), or free space on its file system is under `CATALOG_FREE_FLOOR_GB` (8), the compactor deletes the oldest whole hour (all types and instruments of that hour together, with the hour's `quarantine/` files). It stops once both limits hold. It deletes only hours whose files all have an mtime before the `cutoff` in a valid backup state file. Valid means the JSON parses, `snapshot_id` is not empty, and `cutoff` is not after now. It logs INFO `catalog_retention_deleted` per hour. If it still needs space but nothing is eligible, it logs ERROR `catalog_retention_blocked` with the bytes still needed, at most once per pass. A missing or invalid state file makes nothing eligible, and an invalid one also logs ERROR `backup_state_invalid`.
- **AC-7**: The recorder container is healthy only while three things hold: the heartbeat file is under 30 s old, the newest `order_book_deltas` feather file in the current run was modified under 60 s ago, and free space is at least `CATALOG_HARD_FLOOR_GB` (2). The existing 90 s `start_period` covers the time before the first file exists.
  - While running, if free space falls below the hard floor, the recorder logs CRITICAL `catalog_disk_full` (with `free_bytes`), stops the node, and exits with code 75.
  - On start, while free space is below the floor, it does not build the node. It logs CRITICAL `catalog_disk_full` once, stays unhealthy, and checks every 30 s. Once space returns, it logs INFO `catalog_disk_ok` and starts. There is no crash loop.
- **AC-8**: The nightly backup no longer puts `/data/catalog` in the main snapshot. It always runs a separate catalog step, even when an earlier step failed. That step takes a snapshot tagged `kfs` and `catalog` of `data/`, `quarantine/`, `gaps/`, and `manifests/` only, excluding `live/`, `.compactor/`, and `*.tmp`. On success it writes `catalog_backup.json` (`snapshot_id`, `cutoff` = the UTC time this catalog step began, as ISO 8601) to the `backup-state` volume, through a temp file and rename. The restore drill's `forget` keeps every snapshot tagged `catalog` or `backlog` (`--keep-tag catalog --keep-tag backlog`). Nothing prunes those snapshots.
- **AC-9**: The backlog is the raw feather recorded before the compactor shipped. The disk filled on 2026-10-06, so on 2026-10-08 the nine runs then on the VPS (49.8 GB, 2026-10-03 to 2026-10-06) were deleted there, after a check that B2 snapshot `f0603109` holds every file at the same size. That snapshot carries the tag `backlog`, and any later emptied snapshot gets the same tag. The drill keeps every `backlog` snapshot (`--keep-tag backlog`). The backlog converts once on the laptop: `fetch-catalog.sh --backlog` restores those runs (only their `order_book_deltas`, `trade_tick`, and `crypto_perpetual` files) into `./catalog/live/`, the compactor converts them there, and the resulting `data/`, `quarantine/`, `gaps/`, and `manifests/` files are copied into the VPS catalog, so the next catalog snapshot keeps them forever.
  - **Dropped 2026-10-08.** The backlog is not converted. Fetching it used up the B2 account's daily download cap, which made the VPS backup unhealthy and blocked deploys, and free Tardis days give more varied backtest data than 3.5 days of ours. The `backlog` tag is removed from `f0603109`, so the drill may prune it. The offline mode, the bridge, and `fetch-catalog.sh --backlog` stay in the code, unused; the steps below are kept as the design should it ever run. A future fetch of any size from B2 must not run between 00:00 UTC and the 02:30 UTC backup, because they share the cap.
  - **Offline mode.** On the laptop the compactor runs as `python -m recorder.compactor.main --offline`. No recorder writes there, so no run is the current run: every run counts as ended, including the newest backlog run, and every file counts as closed. The grace wait is 0, retention does not run, the volume marker check and the `/state` mount are skipped, and the process exits once a pass converts nothing more. It exits 0 only when no feather is left under `live/` and the run logged none of `catalog_hour_mismatch`, `compactor_pass_failed`, or `backlog_needs_space`; otherwise it exits 1. Everything else (reorder window, gaps, book carry, the dead run space check, `done.json`) is the normal pass. It runs as `docker compose run --rm --no-deps -v ./catalog:/data/catalog compactor ... --offline`, keeping the 512 MB limit.
  - **Bridge gap.** Then `python -m recorder.compactor.bridge --vps <dir>` writes one `recorder_down` gap per instrument. It starts at that instrument's last written row in `streams` of the laptop `done.json`, and ends at the first delta row (`ts_init`) of the earliest `data/order_book_deltas/<instrument>/` hour in `<dir>`, the VPS catalog's first day, which `fetch-catalog.sh --into ./catalog-vps` fetched apart from `./catalog`. Rules:
    - Only instruments on both sides get a gap. One on only one side gets none and logs WARNING `bridge_instrument_missing`.
    - The empty hours are the hours wholly inside the gap (not the partial hours at its ends). They go into `empty` in `done.json`.
    - The bridge then calls `close_days`. Day closing walks up to the last day covered by converted or `empty` hours, so a day that lies wholly inside the gap (2026-10-07) gets a manifest too.
    - It refuses, logging ERROR and exiting 1, when feather is still left under `live/`, when `<dir>` has no delta hour for an instrument after the start, or when the end is not after the start.
    - A rerun gives the same gap rows, `empty` hours, and manifests.
  - **Copy.** Only `./catalog` is copied (`data/`, `quarantine/`, `gaps/`, `manifests/`), never `./catalog-vps`, so a laptop file never replaces a VPS one. It is a `tar -c` stream over `tailscale ssh` into `kfs-compose run --rm --no-deps -T --entrypoint tar compactor -x -m --no-same-owner -C /data/catalog`, so the files belong to the compactor's user and get a fresh mtime. A fresh mtime means retention (AC-6) cannot delete them before a catalog snapshot newer than the copy holds them. Before the copy, VPS free space must exceed the copy's size plus `CATALOG_FREE_FLOOR_GB`. After it, a sha256 list of the copied files is checked on the VPS. The copy should land before the next 02:30 UTC catalog step, or the snapshot that keeps it comes a night later. The VPS `done.json` never lists the copied hours; retention and fetch read files, so this is fine.

  Once a catalog snapshot newer than the copy exists, and a fetch back of one copied backlog day from it matches the laptop files by sha256, the `backlog` tag is removed with the VPS key (B2 turns the delete of the old snapshot into a hide). The drill may then prune the raw feather; that is the accepted cost (the spec already says the feather cannot be converted again once gone). On the VPS, dead runs (from restarts) are converted by the normal loop, oldest first. Before it starts on a dead run, the compactor needs free space of at least the run's feather bytes × `COMPACTOR_PARQUET_RATIO`, plus `CATALOG_FREE_FLOOR_GB`. If that is missing, it logs ERROR `backlog_needs_space` (with the bytes needed) once per pass and leaves the run untouched. Conversion stays strictly in time order, so a blocked dead run also holds back every newer hour; the recorder's hard floor (AC-7) protects the disk meanwhile. (Decided 2026-10-08: dead runs on the VPS are only short restart runs, and converting the live run past a blocked one would split the hour both runs share.) The 25 GB single file in the backlog is read batch by batch in one pass, and the compactor stays under its 512 MB memory limit. Each restart between runs becomes a `recorder_down` gap.
- **AC-10**: `scripts/fetch-catalog.sh [--into <dir>] <from-date> <to-date>` (both inclusive, UTC days in `YYYY-MM-DD`, from not after to) runs on the laptop. It restores each day's `data/` hours, any `quarantine/` files of those hours, `gaps/<day>.parquet`, and `manifests/<day>.json` into `./catalog` (or `<dir>`), overwriting files already there.
  - **Which snapshot.** Per day, one `restic find --json --tag catalog --host kfs-vps` per name pattern (`<day>T*` for hour files, `<day>.parquet`, `<day>.json`), never one per file. Each file comes from the newest snapshot holding it, by snapshot time from `restic snapshots --json`, so a day partly retired by retention still restores whole.
  - **Missing files.** No gap file is normal (a day without gaps) and is skipped quietly. No manifest logs a warning (the day is still open). A day with no hour file at all ends the script with exit 1, after the other days are done.
  - **Restic mechanics.** Every call uses `--no-lock` (the read only key cannot write lock files) and `--no-cache`. It restores into a temp folder under the target, then moves `data/catalog/*` into place, and runs the container as your uid.
  - **`--backlog`.** Restores the `live/<run>/` stream and instrument files of the three types (AC-1) from every snapshot tagged `backlog`, taking the newest snapshot per path, into `./catalog/live/`. Before restoring, it needs free space of at least the backlog's size plus 10 GB.   - **Credentials.** It runs restic in the `kfs/backup` image, with the repository from `CATALOG_RESTIC_REPOSITORY` in `.env`, the restic password from `secrets/b2/restic_password` (as `RESTIC_PASSWORD`), and a read only B2 key from `secrets/b2/read_key_id` and `secrets/b2/read_application_key` (as `B2_ACCOUNT_ID` and `B2_ACCOUNT_KEY`). These are separate from the laptop's own `RESTIC_REPOSITORY` and `secrets/restic_password`, which belong to the laptop's local backup repository.
  - **Result.** A `ParquetDataCatalog("./catalog")` query returns the rows, and the instrument definition is present.
- **AC-11**: A conversion pass that is killed at any point, then run again, ends with the same set of Parquet files, quarantine files, gap rows, and `incomplete` entries, byte for byte in content. It leaves no `.tmp` file, no duplicate rows, and no deleted feather file whose hour is not in `done.json`. A book state is never applied twice.
- **AC-12**: Rows reach the feather out of `ts_init` order when the recorder's data queue overflows. (In Nautilus 1.231.0, a message that meets a full queue waits in a pending `put()`, and newer messages overtake it.) `ts_init` is stamped before the queue, so it still holds the true arrival order. The compactor repairs this per stream (one type of one instrument):
  - **Reorder window.** It holds the stream's rows read but not yet written. Its **watermark** (an integer in ns) is the newest `ts_init` read so far minus `COMPACTOR_REORDER_WINDOW_S` (600), and the watermark never goes down. Rows at or below the watermark are written in stable `ts_init` order (rows with the same `ts_init` keep their file order). A window of 0 means no reordering: every backwards row is late. If a stream holds more than `COMPACTOR_REORDER_MAX_ROWS` (2,000,000) rows, the watermark moves up to the oldest held row's `ts_init`, and the compactor logs WARNING `catalog_window_capped` with `stream` and `held_rows`. This bounds memory in a burst.
  - **Run end.** A run has ended when it is no longer the current run (the closed file rule). Its held rows are then all written, for every stream of the run at once, so a quiet stream never holds an hour back.
  - **Late rows.** A row whose `ts_init` is below the watermark when read is a late row. Cause `window` means it came from the same run, more than 600 s late. Cause `run_overlap` means it came from a newer run than the rows that set the watermark. A late row is never written to `data/` and never applied to a book. It goes to `quarantine/<table>/<instrument>/<interval of its hour>.parquet`, merged the way gap rows are (read, union, deduplicate on all columns, sort by `ts_init`, write atomically). The hour is added to `incomplete` in `done.json` with its late row count. A late book delta also means the book that built the snapshot of every later hour up to the open one missed it, so each of those hours of that instrument is added to `incomplete` too (with 0 late rows). The carried book of that instrument becomes invalid, and later hours convert without a synthetic snapshot until its next feed `CLEAR`. The compactor logs ERROR `catalog_late_rows` once per stream and hour per pass, with `stream`, `hour_ns`, `rows`, `max_late_s`, and `cause`.
  - **Repaired hours.** An hour where any row was put back in order logs WARNING `catalog_hour_reordered` when it finishes, with `stream`, `hour_ns`, `rows_moved` (summed over each sort, the count of positions where `sort_indices[i] != i`), and `max_late_s` (the largest newest `ts_init` read minus row `ts_init`, in seconds).
  - **Ready rule.** Hour H finishes only once the stream's watermark has passed H + 1 h (the newest row read is at or after H + 1 h + 600 s), or the run has ended. So no row of H inside the window is ever cut off.
  - **Reruns.** Whether a row is in window or late depends only on the stream's row order and the watermark at the start of its file. `done.json` keeps that starting watermark for each file not yet finished. A rerun starts at the stream's earliest unfinished file, with that file's stored watermark (read offsets are kept for finished files only). Rows held in the window from earlier files all belong to hours already done, because their files are finished, so not reading them again changes nothing. A rerun therefore classifies every row the same way (AC-11). A row of a done hour is skipped when it is in window, and merged into quarantine again when it is late.
  - **Gap state.** Gap detection sees only written rows. `streams` in `done.json` stores the last written row of each instrument, never the newest row read, so a pass boundary with rows still held never makes a false gap.
  - **Known limits.** Two messages of one instrument with the same `ts_init` to the nanosecond, one overtaking the other, keep the overtaking order. That is practically impossible, and Kraken's `sequence` is unverified as a tiebreak. A system clock step back of more than 600 s sends the rows after it to quarantine, which fails safe and logs loudly.
- **AC-13**: A `QueueDepthActor` in the recorder reads the data engine's queue depth (`data_qsize()`) every 1 s against its capacity (`LiveDataEngineConfig.qsize`, kept at 100,000).
  - At or above `RECORDER_QUEUE_WARN_PCT` (50) percent, it logs WARNING `recorder_queue_high`. At capacity, it logs ERROR `recorder_queue_full`. Both carry `depth` and `capacity`, and each code is logged at most once per 60 s.
  - The throttle uses the actor's clock (`self.clock.timestamp_ns()`), passed to the pure rule as `now_ns`.
  - In its `on_stop`, it logs the depth: ERROR `recorder_stop_queue_full` at 99% of capacity or more (pending puts can exist just below full), else INFO `recorder_stop_queue`. Nautilus cancels the pending puts at shutdown, so those rows are lost, and this makes the loss visible.
- **AC-14**: The compactor gets less CPU than every other service when the CPUs are busy, and a full core when they are idle. It runs with `cpu_shares: 256` (a quarter of the default cgroup weight), `cpus: 1.0`, and `nice -n 19`. Check on the VPS: the compactor's Compose config shows `cpu_shares: 256` and the host runs cgroup v2 (so the weight applies). The CPU check is that no row is lost or late: no `catalog_late_rows`, and no recorder restart from a stale heartbeat. `recorder_queue_high` is allowed, at peaks and during conversions alike; `recorder_queue_full` that lasts minutes is not. (Learned 2026-10-08: converting a 17 GB dead run kept the queue high for 15 minutes, with 11 hours reordered and none late. Later, at 3.2 GB/h of BTC and ETH with the compactor idle, the queue stayed full, the event loop starved the heartbeat, and `kfs-autoheal` restarted the recorder three times in an hour, dropping the queued rows. Recording BTC only (2.7 GB/h) stopped the restarts, though `recorder_queue_high` still shows at peaks. `cpus: 1.0` stays: the compactor was idle when it failed, so the recorder's own throughput on 2 shared CPUs is the limit.)
- **AC-15**: The recorder finds every hole in the run's message counter (decided 2026-10-08). Every book delta carries `sequence`, one counter per recorder run: it starts at 1, is shared by every instrument of the run, and keeps counting through a websocket reconnect (measured on the VPS, see rationale.md). The `RecordingActor` reads `sequence` and `ts_init` of every delta it receives, for every recorded instrument, and feeds them to a pure `SequenceTracker` (core `marketdata/sequence.py`). `ts_init` and the actor's clock (`self.clock.timestamp_ns()`) are the same live node clock, so the actor's times and row times compare directly.
  - **Start.** The tracker is created in `on_start` before the subscriptions, and starts at the first number it sees, whatever it is: no hole lies before it. Snapshot rows (a `CLEAR` with `F_SNAPSHOT` and its levels) carry numbers and count like any other row. A number below the first one seen, arriving later, is ignored.
  - **Hole.** Any number not seen while a higher one was, however few are missing. A number counts as missing once `RECORDER_SEQUENCE_CONFIRM_S` (600) have passed on the actor's clock since the first higher number arrived; a number that arrives sooner (it was overtaken in the queue, AC-12) ends the suspicion with no record. A hole's `before_ns` is the `ts_init` of the delta numbered just below it, and its `after_ns` that of the delta just above it. The confirm time and the reorder window measure lateness differently (arrival time against row time), but either mismatch fails safe: a row the window keeps is still marked as a gap, or a row the actor accepts is still quarantined as late.
  - **Record.** On confirming a hole the actor logs ERROR `book_sequence_hole` (`run`, `first_missing`, `last_missing`, `missing`, `before_ns`, `after_ns`) at once. The file `live/<run>/sequence_holes.jsonl` is written on the actor's 60 s timer, with one write and one fsync per tick: first the holes confirmed since the last tick (one JSON line each), then `{"checked_to_ns": X}`. X means every hole with `after_ns` below X is in the file: it is the actor's clock minus `RECORDER_SEQUENCE_CONFIRM_S`, lowered to the `before_ns` of the oldest open suspicion. The file's first line, written in `on_start`, is `{"started_ns": …}`. On stop, every open suspicion becomes a hole (fail safe), then `{"closed": true}` is written. A number that arrives after its hole was confirmed logs WARNING `book_sequence_late` and changes nothing (the compactor quarantines that row anyway, AC-12).
  - **No resubscribe (dropped 2026-10-08).** The build gate (Milestone 7) showed that unsubscribing and subscribing the book brings no `CLEAR`, so the actor never resubscribes. A hole stays marked until the next feed `CLEAR` (a reconnect), or else until the run's last row. There are no `RECORDER_RESUBSCRIBE_*` settings, no `recorder_book_resubscribed` log, and no `resubscribed_ns` lines.
  - **File failure.** If the holes file cannot be written, the actor logs CRITICAL `sequence_file_failed` and the recorder stops with exit code 75, the same path as the disk guard (AC-7). A run that keeps recording without its file would hold back every later hour.
  - **Cost.** The tracker keeps seen numbers above the lowest unseen one as intervals, so memory stays small even across a hole of 189k. The gate measures its cost per delta; it must stay under 2% of one VPS core at the 4.3M rows per hour peak.
- **AC-16**: The compactor applies the holes file. It reads only complete lines.
  - **Ready rule.** Hour H of a run is ready only when, besides the existing rule, that run's holes file shows `checked_to_ns` at or after H + 1 h, or `closed`, or the run has ended. In practice this adds no delay, because the hour's feather file only closes at the next rotation. A stalled timer just makes the hour wait.
  - **Fail safe hole.** A run that ended without `closed` (a crash or kill) gets one more hole, with `missing` unknown (logged as −1), from its last `checked_to_ns` to each instrument's last row; an instrument whose last row is before that time gets none. A holes file with no complete line counts as `checked_to_ns` = the run's first row, so the whole run becomes one `sequence` gap. A run with no holes file at all was recorded before this shipped: it converts as before, and INFO `sequence_unchecked_run` is logged once per run.
  - **Every instrument.** A hole marks every instrument with a delta stream in that run, because the shared counter cannot tell whose messages were lost. An instrument with no written row after `before_ns` in that run gets no `sequence` gap (a `recorder_down` gap covers it).
  - **Where the book is cut.** Detection reads the written rows, which the reorder window (AC-12) has already put in `ts_init` order. Rows with `ts_init` at or below `before_ns` are applied to the carried book. From the first row above `before_ns`, the book is invalid until the instrument's first feed `CLEAR` with `ts_init` at or after `after_ns`, which makes it valid again. Rows in between are still written to `data/` (they are real), but not applied, and the hours they touch get no synthetic snapshot.
  - **The gap and the hours.** The `sequence` gap runs from `before_ns` to that `CLEAR`, or to the instrument's last row of the run when no `CLEAR` comes (the next run always starts with one). Every hour from the hour of `before_ns` to the hour of the gap's end is added to `incomplete` in `done.json` with 0 late rows, so the manifest lists it (AC-5). Precedence where spans overlap: `sequence` over `reconnect` over `silence`.
  - **Reruns.** Every hole with `after_ns` below a `checked_to_ns` is already in the file, and later lines only add holes at or after it, so an hour finished under a given `checked_to_ns` always sees the same holes; the fail safe hole comes from an ended run, whose file no longer grows. `done.json` keeps, per run, how far the holes file was read and its last `checked_to_ns`, and per instrument the start of a `sequence` gap still waiting for its `CLEAR`. A rerun gives the same gaps and `incomplete` entries (AC-11).
  - **Cleanup.** A dead run's holes file is deleted only after `done.json` lists every hour of that run, together with or after its last feather file.
  - `missing` stays in the `book_gap` log and the holes file, not in the gap Parquet, whose schema is unchanged.
- **AC-17**: On the VPS, a lone replay of every converted BTC hour of the 24 h after the deploy, sampled at the first row with `ts_init` at or after H + k × 60 s (k = 0 to 59), never shows a crossed book (best bid at or above best ask) or a one sided book. Samples are skipped inside a gap row's span (start and end included), in the first 1 s after a feed `CLEAR`, before the replay's first valid book (no snapshot at the hour start and no `CLEAR` yet), and in hours listed in `incomplete` with late rows (AC-12). This check is the proof that every hole which spoils the book is found.

## Decision

**Chosen option**: Option 2: hourly feather rotation plus a separate compactor service (see [rationale.md](rationale.md)).

The recorder keeps streaming feather, now filtered and rotated hourly. A new `compactor` Compose service turns each closed hour into zstd Parquet in the Nautilus catalog layout, with a book snapshot at its start. It writes gaps and a daily manifest, then deletes old local hours only once the B2 backup holds them.

**Lost messages (2026-10-08)**: the recorder watches the run's message counter and is the detector of record, because it sees every instrument's messages in one order, while the compactor reads one stream at a time. It writes each hole to a small file in the run folder. A resubscribe to cut the damage short was dropped on the build gate (no fresh snapshot came). The compactor turns the holes into `sequence` gaps and incomplete hours (AC-15 to AC-17, see rationale.md).

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
| `live/<run uuid>/sequence_holes.jsonl` | recorder | `started_ns`, then per 60 s tick the confirmed holes and `checked_to_ns`, then `closed` (AC-15) | no |
| `data/order_book_deltas/<instrument>/<interval>.parquet` (`class_to_filename(OrderBookDelta)`) | compactor | one hour of deltas, starting with the snapshot | yes |
| `data/trade_tick/<instrument>/<interval>.parquet` | compactor | one hour of trades | yes |
| `data/crypto_perpetual/<instrument>/<interval>.parquet` | compactor | one row per hour: the newest definition seen up to the end of that hour (carried forward when the hour has none), so any single hour or day is self contained | yes |
| `quarantine/<table>/<instrument>/<interval>.parquet` | compactor | late rows of that hour (AC-12), same schema as `data/`; it exists only for an incomplete hour and belongs to that hour for retention and fetch | yes |
| `gaps/<YYYY-MM-DD>.parquet` | compactor | gap rows, keyed by the gap's `start_ns` day | yes |
| `manifests/<YYYY-MM-DD>.json` | compactor | the day's size and gap totals | yes |
| `.compactor/books/<instrument>/<H>.parquet` | compactor | the carried book at the end of hour H (the two newest hours kept) | no |
| `.compactor/done.json` | compactor | the commit record (below) | no |

**`done.json`** (the single commit point, rewritten through a temp file and rename) holds:
- `hours`: each finished hour, with per stream rows and bytes.
- `carry`: for each instrument, the last hour whose book file is valid, or `null` when the book is invalid.
- `streams`: for each instrument, the last `ts_init` and run seen, so gap detection resumes across passes.
- `watermarks`: for each file not yet finished, the stream's watermark when reading of that file began (AC-12).
- `incomplete`: for each stream, the hours that had late rows, with the late row count (AC-12, AC-5), plus the hours a `sequence` hole spoiled, with 0 (AC-16).
- `holes`: for each run, how far its `sequence_holes.jsonl` was read, and its last `checked_to_ns`; and in `streams`, per instrument, the start of a `sequence` gap still waiting for its `CLEAR` (AC-16).
- `days`: the closed days.

`incomplete` and `watermarks` replace the `overlaps` and `kept` fields of the Milestone 2 build. No `done.json` exists on the VPS yet, so there is nothing to migrate.

An hour counts as done only once its entry is in `done.json`. Parquet files and a book file for an hour that is not yet listed are leftovers of a crash, and a rerun overwrites them.
| `.kfs-volume` | bootstrap | the disk marker (spec 0005) | no |

`<interval>` is `_timestamps_to_filename(H, H + 3_599_999_999_999)` from `nautilus_trader.persistence.catalog.parquet`. Hours are disjoint and contiguous, so the catalog's disjoint interval check always passes.

**Gap row** (Parquet schema): `instrument_id string`, `kind string` (`reconnect` | `recorder_down` | `silence` | `sequence`), `start_ns uint64`, `end_ns uint64`. Unique on `(instrument_id, kind, start_ns)`, and sorted by `start_ns`. Each new gap is merged into its day file (read, union, deduplicate, sort, write atomically), so a rerun gives the same rows (AC-11). A gap that crosses midnight stays whole in its start day's file. Manifests clip its seconds to each day, reading every gap file up to their own day, so a gap longer than a day counts on each day it covers. Known limit: the VPS's first day (2026-10-08) closes before the bridge gap is copied in, so its manifest leaves out that day's part of the bridge gap; the `gaps/` file stays the record.

**Manifest** (JSON): `{"date": "YYYY-MM-DD", "total_bytes": int, "streams": [{"instrument_id", "type", "rows", "bytes", "hours", "late_rows", "incomplete_hours": [hour_ns]}], "gaps": [{"instrument_id", "kind", "seconds"}]}`. If `incomplete` changes for a day already closed (a late row found later), the compactor writes that day's manifest again, atomically.

**Backup state** (`/state/catalog_backup.json` on the new `backup-state` volume): `{"snapshot_id": str, "cutoff": "YYYY-MM-DDTHH:MM:SSZ"}`.

### Lifecycle of one hour

`open` (the recorder writes it) → `closed` → `ready` → `converted` (Parquet renamed in place) → `done` (listed in `done.json`) → `feather deleted` → `backed up` (file mtime < cutoff) → `retired` (deleted by retention). A failure at any step leaves the hour at its previous state, and the next pass retries it.

- **Run**: a folder `live/<uuid>/` written by one recorder process. Runs are ordered by the earliest `ts_init` across their files. The **current run** is the run whose feather file names hold the newest creation time (the `<ns>` in each name), and there is only one recorder. Not the folder `st_ctime`: deleting a file in a dead run changes that folder's ctime.
- **Closed file**: a feather file is closed when its run is not the current run (the process that wrote it is gone), or when the same run folder has a newer file for the same type and instrument (it rotated). The current run's newest file per stream is never closed, however quiet it is. File names are the Nautilus stream names (`order_book_deltas/`, `trade_tick/`, and `crypto_perpetual_<ns>.feather`), which differ from the catalog's `class_to_filename` names.
- **Ready hour**: hour H is ready when now ≥ H + 1 h + `COMPACTOR_GRACE_S`, and the compactor's stream has passed H + 1 h + `COMPACTOR_REORDER_WINDOW_S`: it has read a closed file with a row at or after that time, for every delta and trade stream that had rows in H (AC-12). Once its run has ended, the last hour of a dead run is ready too. The instrument stream does not gate readiness. It holds the whole venue's list (about 226 rows), refreshed every 60 minutes from the recorder's start, so its file rotates up to an hour late.
- **Instrument file**: the one exception to the closed file rule. The compactor reads the current run's `crypto_perpetual_<ns>.feather` read only, up to its last complete batch, so the first hours of a run have a definition. It never deletes or commits that file while it is open.

**One pass, in time order.** The compactor reads closed feather files in order of (run order, then file start) and never scans a file twice. A small index in `done.json` records, per finished file, how far it was read. Rows pass through each stream's reorder window (AC-12) into the open hour's writers. When the watermark passes H + 1 h, it finishes hour H: snapshot, verify the counts, rename, save the book, then commit to `done.json`. So the 25 GB file is read once, from start to end, and becomes about 45 hours.

### Conversion rules

- **Reading**: `pyarrow.ipc` record batches, one at a time. The stream holds about 1.5 rows per batch, so batches are gathered into chunks of at most 8,192 rows before each flush to the Parquet writer and the book (Milestone 0: 65,536 row chunks were killed at 512 MB; 8,192 held 343 MB on a 2 GB file). A truncated final message (a killed recorder) ends the file at its last complete batch, and the rest lies inside the next `recorder_down` gap.
- **Hour assignment**: by `ts_init`. Rows of one feed message (up to its `F_LAST`) share a `ts_init`, so a group never splits across hours.
- **Reorder window** (AC-12): each stream keeps the rows above its watermark as Arrow chunks. When the watermark moves, the held rows and the new chunk are concatenated in that order ([held, new], so file order breaks ties), put in stable `ts_init` order (`pyarrow.compute.sort_indices`, which is stable), and the rows at or below the watermark are written. Fast path: when a chunk's lowest `ts_init` is at or above the newest held one, no sort runs. Chunk size never changes the output. Hour assignment, gap detection, and the book all see rows only after this step. A stream that never goes backwards is written in its file order, unchanged. At the 2 GB/h peak, 600 s holds about 0.5M delta rows (about 30 to 60 MB). Rows of an hour already in `done.json` are skipped on a rerun only when the window classifies them as in window, and late ones are merged into quarantine again.
- **Book carry**: per instrument, a `nautilus_pyo3.OrderBook(L2_MBP)` is restored (`ArrowSerializer.deserialize` returns pyo3 deltas) from `.compactor/books/<instrument>/<H−1>.parquet` when `carry` in `done.json` says H − 1 is valid. Every delta of the hour is applied to it, and the result is saved as `<H>.parquet` before the commit.
- **Snapshot at hour start** (AC-3): written when the carry is valid for H − 1, which means no gap and no invalid mark between the end of H − 1 and H. Otherwise the hour gets no synthetic snapshot. That instrument's rows before its next feed `CLEAR` are still written, because they are real data and sit inside the gap the detector reports. They are not applied to a book. The carry becomes valid again from that `CLEAR` on.
- **First hour ever**: there is no carry, so the hour starts without a synthetic snapshot. The run's first feed `CLEAR` (checked in Milestone 0) starts the carry.
- **Trades**: copied as they are, with no snapshot.
- **Instruments**: one `CryptoPerpetual` row per hour (see the layout table).
- **Verification before delete** (AC-2): rows in each Parquet = source rows of the hour + snapshot rows, checked by reading the Parquet footer.
- **Schema**: the Parquet table keeps the feather stream's Arrow schema and its metadata, which are what Nautilus's catalog reader decodes (Milestone 0 proves this). Test: `catalog.order_book_deltas(instrument_ids=[...], start=H, end=H+1h)` returns the rows.
- **Memory and CPU**: one batch, the reorder windows, and the carried books are resident. The service has `mem_limit: 512m`, `cpus: 1.0`, `cpu_shares: 256`, and runs at `nice 19` (AC-14). Each container has its own cgroup, so `nice` only orders work inside the compactor. The cgroup weight is what lets the recorder and the trader win the CPU over the compactor.

### Value sourcing

| Action | Value produced | Source |
|---|---|---|
| plan | closed files, read order | listing of `live/`, the newest `<ns>` in the feather file names (the current run), sibling files per stream, the run order (earliest `ts_init`, kept in `done.json` once read), the read offsets in `done.json` |
| plan | hour ready | `now`, `COMPACTOR_GRACE_S`, and the stream position (the first row at or after H + 1 h, or the end of a dead run) |
| convert | Parquet file path | `class_to_filename`, `urisafe_identifier`, `_timestamps_to_filename(H, H + 1 h − 1 ns)` (Nautilus 1.231.0) |
| convert | snapshot deltas | `.compactor/books/<instrument>/<H−1>.parquet` when `carry` in `done.json` is valid for H − 1; `sequence` from the last applied delta; `ts = H` |
| convert | instrument row | the newest `CryptoPerpetual` row for that instrument read up to H + 1 h (closed files, or the current instrument file up to its last complete batch), carried in memory and in `done.json` |
| verify | expected rows | the count of source rows routed to the hour, minus its late rows, plus the snapshot rows written |
| reorder | watermark | the newest `ts_init` read in the stream minus `COMPACTOR_REORDER_WINDOW_S`, never lower than before; at a file's start, `watermarks` in `done.json` |
| reorder | late or in window, and its cause | the row's `ts_init` against the watermark; the run of the row against the run that set the watermark |
| reorder | rows moved, max lateness | per hour, rows whose position changed in the sort, and the largest (newest `ts_init` read − row `ts_init`) |
| quarantine | file path | the same `class_to_filename`, `urisafe_identifier`, and `_timestamps_to_filename` as `data/`, under `quarantine/` |
| queue watch | depth and capacity | a closure over the built node, `lambda: node.kernel.data_engine.data_qsize()`, passed to the actor in `recorder/main.py` (it is called only after `node.build()`); capacity is `qsize` of the `LiveDataEngineConfig` object that `build_config` passes to the node |
| queue watch | throttle time | `self.clock.timestamp_ns()` of the actor |
| reorder | window row cap | `COMPACTOR_REORDER_MAX_ROWS` |
| gaps | last row per instrument | the last **written** row, stored in `streams` |
| queue watch | warn level | `RECORDER_QUEUE_WARN_PCT` |
| detect gaps | gap start and end | `ts_init` of the neighbouring rows, the last `ts_init` and run per instrument in `done.json` (`streams`), `BOOK_SILENCE_GAP_S` |
| backlog check | bytes needed | the dead run's feather bytes × `COMPACTOR_PARQUET_RATIO` + `CATALOG_FREE_FLOOR_GB` |
| manifest | bytes, rows, hours | per hour stats in `done.json` (`hours`) |
| manifest | gap seconds | every `gaps/<date>.parquet` up to the day (a gap may span several days), each gap clipped to the day |
| retention | catalog bytes | sum of file sizes under `data/` |
| retention | free bytes | `os.statvfs(catalog_path)` |
| retention | backed up or not | `cutoff` in a valid `/state/catalog_backup.json` compared with each file's mtime |
| recorder health | stream freshness | mtime of the newest file under `live/<current run>/order_book_deltas/` (current run = the run with the newest `<ns>` in its file names) |
| recorder health and floor | free bytes | `os.statvfs(catalog_path)` |
| backup state | cutoff | `date -u` taken at the start of the catalog step in `docker/backup/run.sh` |
| fetch script | repository and keys | `CATALOG_RESTIC_REPOSITORY` in `.env`, `secrets/b2/restic_password`, `secrets/b2/read_key_id`, `secrets/b2/read_application_key` |
| fetch script | which snapshot per file | the newest snapshot tagged `catalog` that `restic find --tag catalog` reports for that path |
| fetch script | which files per day | the 24 hour intervals of the day under `data/` and `quarantine/`, plus `gaps/<day>.parquet` and `manifests/<day>.json` |
| fetch script `--backlog` | which runs | every snapshot tagged `backlog`, its `live/<run>/` stream and instrument files of the three types |
| offline conversion | current run | none: `--offline` makes every run ended and every file closed |
| bridge gap | start | the instrument's last written row in `streams` of the laptop `done.json` |
| bridge gap | end | the first `ts_init` of the earliest `data/order_book_deltas/<instrument>/` hour in `./catalog-vps` (the VPS catalog's first day, fetched with `--into`) |
| bridge gap | empty hours | the hours wholly between start and end |
| copy to the VPS | file mtime | the time of extraction (`tar -m`), never the laptop mtime |
| sequence watch | counter and row time | `delta.sequence` and `delta.ts_init` of each `OrderBookDelta` in the batches `RecordingActor.on_order_book_deltas` receives |
| sequence watch | confirm time | `self.clock.timestamp_ns()` of the actor, `RECORDER_SEQUENCE_CONFIRM_S` |
| sequence watch | holes file path | `<catalog_path>/live/<node.kernel.instance_id>/sequence_holes.jsonl`, the run id passed to the actor from `recorder/main.py` after `node.build()` (the gate confirms the run folder is named by `instance_id`) |
| sequence gap | start, end, missing | `before_ns` and `missing` from the holes file; end = `ts_init` of the instrument's first feed `CLEAR` at or after `after_ns` in its written rows, else its last row of the run |
| sequence gap | book cut | the instrument's written rows (after the reorder window) against `before_ns` |
| fail safe hole | span | the run's last `checked_to_ns` (or its first row when the file has no complete line) to each instrument's last row |
| sequence gap | instruments hit | every instrument with an `order_book_deltas` stream in the run |
| sequence ready gate | checked time | the last `checked_to_ns` (or `closed`) in the run's holes file, kept in `done.json` `holes` |

### Code placement

- `packages/core/src/trading_core/marketdata/` (pure, no I/O, docstrings, unit tests in core):
  - `hours.py`: the hour math (hour of a `ts_init`, hour bounds, the interval for a file name), the ready rule (now, grace, stream position, window), the day close rule, the backlog space check, and the watermark rule with the late test (AC-12).
  - `queue_depth.py`: given depth, capacity, warn percent, and the time each code was last logged, it returns the code to log now, if any (AC-13).
  - `gaps.py`: given an ordered stream of `(ts_init, is_clear, run)` per instrument and the state carried from earlier passes, it returns the gap records and the new state. It streams across batches, and handles the dedupe of reconnect versus silence.
  - `sequence.py`: the `SequenceTracker`. Given `(sequence, ts_init)` pairs in arrival order and `now_ns`, it returns the holes confirmed and the `checked_to_ns` value; seen numbers are kept as intervals (AC-15). Also the rule that turns holes plus an instrument's `CLEAR` times into `sequence` gaps and incomplete hours (AC-16), used by `gaps.py`.
  - `retention.py`: given the hours with their bytes and newest mtime, the cutoff, catalog bytes, free bytes, cap, and floor, it returns the hours to delete and the bytes still needed when it is blocked. It also validates a parsed backup state.
  - Typed exceptions: `BackupStateInvalid`, `HourCountMismatch`. (`HourOverlapError` goes: late rows are a normal path now, not an error.)
- `apps/recorder/src/recorder/compactor/` (adapter): `main.py` (loop, settings, logging), `convert.py` (pyarrow, the reorder window, and OrderBook), `store.py` (paths, atomic writes, `done.json`, quarantine merges, the backup state reader).
- `apps/recorder/src/recorder/actor.py`: `RecordingActor` gains `on_order_book_deltas`, the tracker, the holes file writer (`sequence_holes.py` beside it), a 60 s timer for `checked_to_ns`, and the stop path that closes the file (AC-15).
- `apps/recorder/src/recorder/compactor/`: `store.py` reads a run's holes file up to its last complete line and ignores it in feather discovery; `main.py` passes each run's holes to the delta streams and applies the ready gate; `convert.py` invalidates the carry at a hole (AC-16).
- `apps/recorder/src/recorder/queue_depth.py`: the `QueueDepthActor` (a 1 s timer, the probe injected from `main.py`, and the stop check). The build confirms that Nautilus stops actors before the data engine cancels its pending puts. If it does not, the stop check moves into `main.py` before `node.stop()`.
- `apps/recorder/src/recorder/`: `main.py` (`StreamingConfig`, and the wait at start while below the floor), `disk.py` (the free space floor, next to `volume.py`), `health.py` (the container health check), and a `DiskGuardActor` that checks free space every 10 s. Below the floor it logs CRITICAL, flushes the logs, and stops the process with exit code 75 (by sending itself `SIGTERM`, so the node shuts down cleanly, with `os._exit(75)` after 20 s as a fallback). The restart then lands in the wait at start.

### Compose and backup changes

- `compose.yaml`, new service `compactor`. It uses the recorder image and runs `python -m recorder.compactor.main`. Environment: base env, `PROCESS_NAME: compactor`, `CATALOG_PATH`, `BACKUP_STATE_PATH: /state/catalog_backup.json`, and the settings below. Volumes: `catalog:/data/catalog` (read and write), `backup-state:/state:ro`. No network access is needed; it gets `network_mode: none`. `mem_limit: 512m`, `cpus: 1.0`, `cpu_shares: 256`, and the command runs under `nice -n 19` (AC-14). It has no secrets. Health: a heartbeat file touched by the loop, under 180 s old.
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
- A late row is never written to `data/` and never applied to a book. It is never dropped either: it sits in `quarantine/` until the backup holds it.
- The compactor never touches a file that is not closed, and never reads a file twice. The one exception is the current run's instrument file, which it reads read only and never deletes while open.
- The recorder has no execution client and no secrets (unchanged). The compactor has no network and no secrets.

### Security model

All data here is public market data. The compactor gets no secrets and no network. The backup keeps its existing B2 key (no delete right). The laptop fetch uses a new B2 application key limited to `listBuckets`, `listFiles`, `readFiles` on this bucket. It is stored as `secrets/b2/read_key_id` and `secrets/b2/read_application_key` (gitignored, never committed). The B2 repository's restic password is `secrets/b2/restic_password`, and the repository is `CATALOG_RESTIC_REPOSITORY` in the gitignored `.env`. The script reads these files on the host and passes them to the container only as environment variables (`docker run -e NAME`), never as argument values. You create the key in the B2 console; it never goes to the VPS.

### Configuration required

Through `trading_core.settings`, validated at start:
- `CATALOG_CAP_GB` (compactor, default 30): the cap on `data/` size.
- `CATALOG_FREE_FLOOR_GB` (compactor, default 8): the free space retention keeps.
- `CATALOG_HARD_FLOOR_GB` (recorder, default 2): below this, the recorder stops, or waits at start. It must be below `CATALOG_FREE_FLOOR_GB`. Each process validates the pair at start, because both settings are shared through `base-env`.
- `COMPACTOR_PARQUET_RATIO` (compactor, default set from the Milestone 1 measurement, as a Decimal): Parquet bytes per feather byte, used by the backlog space check.
- `BOOK_SILENCE_GAP_S` (compactor, default 10): the silence gap threshold.
- `COMPACTOR_GRACE_S` (compactor, default 300): the wait after an hour ends before it counts as ready.
- `COMPACTOR_INTERVAL_S` (compactor, default 60): the time between passes.
- `COMPACTOR_REORDER_WINDOW_S` (compactor, default 600, at least 0): how late a row may be and still be put back in place (AC-12).
- `COMPACTOR_REORDER_MAX_ROWS` (compactor, default 2,000,000, at least 1): the most rows one stream's window may hold (AC-12).
- `RECORDER_QUEUE_WARN_PCT` (recorder, default 50, from 1 to 99): the queue depth that logs `recorder_queue_high` (AC-13).
- `RECORDER_SEQUENCE_CONFIRM_S` (recorder, default 600, at least 1): how long a missing number may be overtaken before it counts as lost (AC-15). The same 600 s as the reorder window, so a row the compactor would still put back in place is never called lost.
- `BACKUP_STATE_PATH` (compactor, default `/state/catalog_backup.json`).

### Logs

| Code | Level | Process | When |
|---|---|---|---|
| `catalog_hour_converted` | INFO | compactor | each hour written, with rows and bytes per stream and the duration |
| `book_gap` | WARNING | compactor | each gap (AC-4) |
| `catalog_day_closed` | INFO | compactor | each manifest (AC-5) |
| `catalog_retention_deleted` | INFO | compactor | each hour retired |
| `catalog_retention_blocked` | ERROR | compactor | space needed but nothing is backed up |
| `catalog_hour_reordered` | WARNING | compactor | an hour had rows put back in order (AC-12) |
| `catalog_late_rows` | ERROR | compactor | rows later than the window, or from an overlapping run, went to quarantine (AC-12) |
| `catalog_window_capped` | WARNING | compactor | a stream's window hit `COMPACTOR_REORDER_MAX_ROWS` (AC-12) |
| `catalog_hour_mismatch` | ERROR | compactor | Parquet rows do not match the source (AC-2) |
| `backlog_needs_space` | ERROR | compactor | a dead run cannot convert yet (AC-9) |
| `backup_state_invalid` | ERROR | compactor | the backup state file is unreadable or wrong (AC-6) |
| `compactor_pass_failed` | ERROR | compactor | any other exception in a pass, with the hour; the loop continues |
| `bridge_instrument_missing` | WARNING | compactor (bridge, laptop) | an instrument is in the backlog or the VPS catalog but not both, so it gets no bridge gap (AC-9) |
| `catalog_disk_full` | CRITICAL | recorder | AC-7 |
| `catalog_disk_ok` | INFO | recorder | space returned after a wait (AC-7) |
| `recorder_queue_high` | WARNING | recorder | the data queue is at or above the warn level (AC-13) |
| `recorder_queue_full` | ERROR | recorder | the data queue is full, so rows are being reordered (AC-13) |
| `recorder_stop_queue_full` | ERROR | recorder | the recorder stopped with the queue at 99% or more, so pending rows were likely lost (AC-13) |
| `recorder_stop_queue` | INFO | recorder | the queue depth at stop, below 99% (AC-13) |
| `book_sequence_hole` | ERROR | recorder | a confirmed hole in the run's message counter (AC-15) |
| `book_sequence_late` | WARNING | recorder | a number arrived after its hole was confirmed (AC-15) |
| `sequence_file_failed` | CRITICAL | recorder | the holes file could not be written; the recorder exits 75 (AC-15) |
| `sequence_unchecked_run` | INFO | compactor | a run has no holes file (recorded before AC-15 shipped) (AC-16) |

Feature 12 alerts on the ERROR and CRITICAL codes, and on `book_gap` with a duration over 60 s.

### Critical test scenarios

- Happy path: a fixture run with two hours of deltas and trades converts to two Parquet hours, the feather files are deleted, and a lone replay of hour two matches the full replay. Verifies **AC-2**, **AC-3**.
- Restart: two runs with a 40 s hole and a mid run `CLEAR` produce one `recorder_down` gap and one `reconnect` gap, and the hour after the restart starts at the feed snapshot. Verifies **AC-4**, **AC-3**.
- Silence: 12 s without a delta for ETH gives one `silence` gap, and 9 s gives none. Verifies **AC-4**.
- Crash: kill the pass at each commit step in turn (after the rename, after the book save, after `done.json`, during the feather delete), then rerun. The files and gap rows are identical, with no `.tmp`, no duplicates, and no book applied twice. Verifies **AC-11**.
- Reorder within the window: a fixture stream whose message groups are shuffled by up to 590 s converts to Parquet identical to the same stream in `ts_init` order, with no late rows, the same gaps, and the same carried book. It logs `catalog_hour_reordered`. Verifies **AC-12**, **AC-4**.
- Late row: one message group 700 s late goes to `quarantine/`, the hour converts without it and is listed in `incomplete`, the manifest counts it, the count check passes, and the next hour has no synthetic snapshot until the feed `CLEAR`. It logs `catalog_late_rows` with `cause=window`. Verifies **AC-12**, **AC-2**, **AC-5**.
- Late row for a committed hour: a row of hour H that arrives after H is in `done.json` goes to quarantine, and H's Parquet is left unchanged. Verifies **AC-12**.
- Overlap: two runs whose rows interleave give late rows with `cause=run_overlap`, and nothing else is lost. Verifies **AC-12**.
- Crash with late rows: kill the pass after a late row is found and before the commit, then rerun. The data, quarantine, and `incomplete` entries are identical to an uninterrupted pass. Verifies **AC-11**, **AC-12**.
- Hour boundary: a row of H that is 500 s late, read after rows of H + 1 h, lands in H's file, because H finishes only once the watermark passes H + 1 h. Verifies **AC-12**.
- Late row after the next snapshot: a delta of H found while H + 1 is open marks both H and H + 1 incomplete. Verifies **AC-12**, **AC-5**.
- Chunk size: the same shuffled stream read with chunks of 1 row and of 8,192 rows gives identical bytes. Verifies **AC-12**.
- Pass boundary: a pass that ends with rows held, then the next pass, gives no false `silence` gap and the same gap rows as one long pass. Verifies **AC-4**, **AC-11**.
- Run end: a dead run whose trade stream went quiet hours before its end still finishes its last hour. Verifies **AC-12**.
- Window cap: with the cap set to 1,000, a burst logs `catalog_window_capped` and memory stays bounded. Verifies **AC-12**.
- Queue watch: with a fake probe and clock, depth 49% logs nothing, 50% logs one `recorder_queue_high`, full logs `recorder_queue_full`, each repeats only after 60 s, a stop at 99% logs `recorder_stop_queue_full`, and a stop at 10% logs `recorder_stop_queue`. Verifies **AC-13**.
- Mismatch: a forced row count difference keeps the feather and logs `catalog_hour_mismatch`. Verifies **AC-2**.
- Quiet live file: the current run's trade file unchanged for 20 minutes is never treated as closed. Verifies **AC-2**.
- Truncated tail: a fixture file cut in the middle of a batch converts up to its last complete batch. Verifies **AC-4**.
- Midnight: a `recorder_down` gap from 23:50 to 00:20 lies in the first day's gap file, and each manifest counts its own part. A day with two empty hours inside a gap still closes. Verifies **AC-5**.
- Retention: with the cap exceeded and the cutoff before the oldest hour, nothing is deleted and `catalog_retention_blocked` is logged. With the cutoff moved forward, the oldest hours go first, whole, until both limits hold. A cutoff in the future logs `backup_state_invalid`. Verifies **AC-6**.
- Disk floor: a fake `statvfs` below 2 GB makes the guard log `catalog_disk_full` and exit 75. At start, it waits and then logs `catalog_disk_ok` once space returns. The health check fails on a stale stream file. Verifies **AC-7**.
- Types: a new run folder contains only the three types, rotated at the hour. Verifies **AC-1**.
- Memory and backlog: converting a 2 GB synthetic file inside the 512 MB container succeeds and reads the file once, at the 2 GB/h row rate with a full 600 s window held. With too little free space, the run is left alone and `backlog_needs_space` is logged. Verifies **AC-9**.
- Fetch: on a local restic repository holding two days in different snapshots, a two day fetch restores both, and the instrument query works. A day whose hour is in two snapshots comes from the newer one. A day with no hours exits 1. Verifies **AC-10**.
- Offline: two fixture runs converted with `--offline` both finish, including the last hour of the newest run, no feather is left, and the exit code is 0. A forced mismatch exits 1. Verifies **AC-9**.
- Bridge: after an offline conversion ending on day D, a fetched VPS hour on D + 2 gives one `recorder_down` gap per instrument, the empty hours of D + 1, and manifests for D and D + 1. A rerun changes nothing. An instrument on only one side logs `bridge_instrument_missing`. Verifies **AC-9**, **AC-5**.
- Long gap: a `recorder_down` gap of 50 h is counted on each of the three days it covers. Verifies **AC-5**.
- Backup: a run against a local restic repository creates the `catalog` tagged snapshot without `live/` and writes the state file. Verifies **AC-8**.
- Sequence tracker: numbers 1 to 1,000 with 400 to 410 missing confirm one hole (`first_missing` 400, `missing` 11) only after 600 s on the fake clock; 400 to 410 arriving after 500 s confirm nothing; one missing number is a hole; two instruments' interleaved numbers with none missing give no hole; a hole of 189,000 keeps memory flat. Verifies **AC-15**.
- Holes file: a stop with an open suspicion writes it as a hole, then `closed`; `checked_to_ns` never passes the oldest open suspicion. Verifies **AC-15**.
- Sequence gap: a fixture run of two instruments with a hole and a feed `CLEAR` 40 s later per instrument gives one `sequence` gap each, ending at that instrument's `CLEAR`, no `reconnect` gap at that `CLEAR`, no `silence` inside it, no snapshot in the hours it touches, and those hours in `incomplete`. Verifies **AC-16**, **AC-4**, **AC-5**.
- Ready gate: an hour whose run's `checked_to_ns` is still below H + 1 h waits; a dead run without `closed` gets the fail safe hole. A run with no holes file converts as before and logs `sequence_unchecked_run`. Verifies **AC-16**.
- Crash with holes: kill the pass mid gap, rerun; the gap rows and `incomplete` are identical. Verifies **AC-11**, **AC-16**.
- Book cut: an instrument row 30 s before `before_ns` that the window put back in place is applied; the first row after `before_ns` is not; the book is valid again from the `CLEAR` at or after `after_ns`, and a `CLEAR` between `before_ns` and `after_ns` does not end the gap. Verifies **AC-16**.
- Edges: a holes file with only a torn line makes the whole run one `sequence` gap; an instrument quiet after `before_ns` gets no `sequence` gap; a tracker starting at 5,000 reports no hole below it. Verifies **AC-15**, **AC-16**.
- File failure: a holes file write that raises logs `sequence_file_failed` and exits 75. Verifies **AC-15**.
- Real data: the BTC 15:00 hour of 2026-10-08 (kept in the session evidence) replayed with its holes gives no crossed or one sided sample outside gaps. Verifies **AC-17**.

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

**Milestone 4: out of order rows (decided 2026-10-08)**
9. Core: the watermark rule, the late test, and the window aware ready rule in `marketdata/hours.py`, plus `marketdata/queue_depth.py`, with unit tests. Satisfies **AC-12**, **AC-13**.
10. Compactor: the reorder window in `convert.py`, quarantine merges in `store.py`, and `incomplete` and `watermarks` in place of `overlaps` and `kept` (remove `HourOverlapError` from `marketdata/errors.py`). Then the manifest's late counts and the count check. Satisfies **AC-12**, **AC-2**, **AC-4**, **AC-5**, **AC-11**.
11. Recorder: the `QueueDepthActor`, wired in `main.py`, with the stop check. Compose: the compactor's `cpu_shares: 256` and `nice -n 19`. Satisfies **AC-13**, **AC-14**.
12. Laptop stress check (brief, then stack down): load the CPU until the recorder logs `recorder_queue_full`, and confirm the hour converts with `catalog_hour_reordered` and no late rows. Satisfies **AC-12**, **AC-13**.

**Milestone 5: disk safety and backup**
13. Recorder `disk.py`, `DiskGuardActor`, the floor check at start, and `recorder.health`. Satisfies **AC-7**.
14. Backup `step_catalog` (with `quarantine/`), the `backup-state` volume, and `--keep-tag catalog` in the drill. Satisfies **AC-8**.
15. Core `marketdata/retention.py` and the compactor retention step, deleting an hour's quarantine files with it. Satisfies **AC-6**.

**Milestone 6: laptop and VPS**
16. `scripts/fetch-catalog.sh` (with the day's quarantine files, `--into`, and `--backlog`) and the read only B2 key files under `secrets/b2/`. The compactor's `--offline` mode, `recorder.compactor.bridge`, day closing over `empty` days, and manifests reading every earlier gap file, with the Fetch, Offline, Bridge, and Long gap tests. Satisfies **AC-10**, **AC-9**, **AC-5**.
17. Deploy to the VPS, after checking that `/srv/kfs/catalog/.compactor` does not exist yet. (Deployed 2026-10-08; the backlog steps below were dropped the same day, see AC-9.) After the VPS's first catalog snapshot: on the laptop, fetch the `backlog` runs from B2, convert them offline, fetch the VPS's first day into `./catalog-vps` and write the bridge gap, copy `./catalog` into the VPS catalog (space check, fresh mtime, sha256 check), and remove the `backlog` tag once a newer catalog snapshot fetches back identical. Then delete the laptop's `./catalog` and `./catalog-vps`. Confirm that `df -h /srv/kfs` stays flat over 24 h, that a manual backup writes the state file, and that no `catalog_late_rows` is logged while the compactor works through a dead run (AC-14). Satisfies **AC-9**, **AC-6**, **AC-8**, **AC-14**.

**Milestone 7: lost messages (decided 2026-10-08)**

Tracer Bullet again: one hole goes from the recorder's counter to a `sequence` gap in the catalog first, then the edge cases thicken it (the resubscribe was dropped on the gate).

18. Gate (throwaway, on the laptop, brief run, then the stack down). Prove three things on Nautilus 1.231.0:
    - Unsubscribing and resubscribing the book from the `RecordingActor` makes the Kraken feed send a `CLEAR` with `F_SNAPSHOT` within 10 s, `sequence` keeps counting up with no reset, and the trade stream keeps flowing. If not, drop the resubscribe (AC-15 says so) and carry on. (Result 2026-10-08: no `CLEAR` came, so it was dropped.)
    - The run folder under `live/` is named by `node.kernel.instance_id`.
    - `on_order_book_deltas` plus the tracker costs under 2% of one core at 4.3M rows per hour (replay a recorded hour through the handler at that rate). If not, back to `/architect`.
    Satisfies **AC-15** (as preconditions).
19. Core `marketdata/sequence.py` (tracker and the gap rule) and the `sequence` kind in `gaps.py`, with unit tests. Satisfies **AC-15**, **AC-4**.
20. Recorder: the tracker in `RecordingActor`, the holes file, the 60 s timer, the stop path, and the logs (the resubscribe was dropped on the gate in task 18). Satisfies **AC-15**.
21. Compactor: read the holes file, the ready gate, `sequence` gaps for every instrument of the run, the carry invalid until the `CLEAR`, the `incomplete` hours, the fail safe hole, cleanup, and the rerun test. Satisfies **AC-16**, **AC-4**, **AC-5**, **AC-11**.
22. The AC-3 replay test samples at the first row after every 60 s, and the real data test on the 15:00 hour. Satisfies **AC-3**, **AC-17**.
23. Deploy. After 24 h, replay every converted BTC hour of that day and count crossed or one sided samples outside gaps; count `book_sequence_hole`. Satisfies **AC-17**, **AC-15**.

## Migration plan

**Strategy**: in place, one deploy. The old run folders are just dead runs to the new compactor.
**Phases**:
1. Deploy the new recorder and compactor. The recorder starts a new run folder with hourly files. The compactor converts the old run folders still on the VPS (recorded since 2026-10-08) in the background, oldest first.
2. The first nightly catalog step after the deploy uploads `data/` and writes the state file. From then on, retention can work.
3. Dropped 2026-10-08 (AC-9): the backlog converts on the laptop from B2 and is copied in. This waits for the VPS's first catalog snapshot, because the bridge gap needs the VPS's first day from B2.
**Rollback**: redeploy the previous sha. The old recorder ignores `data/` and keeps streaming into `live/`. Stop the compactor service. Parquet already written stays valid.
**Risks**:
- **CPU.** The backlog conversion competes for CPU with the trader and the recorder on a 2 CPU, 4 GB box. It is capped at one core with a quarter of the cgroup weight (AC-14). If the recorder still falls behind, `recorder_queue_high` shows it live, and the reorder window repairs up to 600 s of lag. Progress shows in `catalog_hour_converted`.
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
- Each hour finishes 10 minutes later (the reorder window), and the compactor holds 30 to 60 MB more memory at peak.
- Rows more than 600 s late make an incomplete hour, which is missing those rows in `data/`. The book carry then stays off until the next feed `CLEAR`, which only comes with a reconnect or a restart, so that can last a long time. The rows are safe in `quarantine/`, but nothing merges them back. A research reader has to check `incomplete` in the manifest.
- Known limits of the repair (AC-12): two messages of one instrument with the same `ts_init` to the nanosecond keep the overtaking order; a clock step back of more than 600 s sends the following rows to quarantine; two identical late rows collapse into one in the quarantine dedupe.
- Rows still waiting in the queue when the recorder stops are lost. `recorder_stop_queue_full` reports it, but the hole sits before the run's last row, so no gap covers it unless it lasts 10 s or more.
- The sequence watch adds a Python call per delta batch to the recorder's event loop, the very loop that starved under BTC and ETH. The gate caps it at 2% of a core, but it is not free.
- A hole on any instrument marks every instrument of the run, so a busy instrument's loss spoils a quiet one's data too. With BTC only this costs nothing.
- With no resubscribe, a hole's `sequence` gap lasts until the instrument's next feed `CLEAR` at or after the hole, which only a reconnect brings, or else until the run's last row (the next run starts fresh). On a stable connection that can mark many hours incomplete. AC-17's replay check measures how often that happens.
- A crash marks up to 10 minutes before its end as a `sequence` gap, whether or not anything was lost there.
- The cause of the losses is still unknown: this design finds and limits them, it does not prevent them.
- The catalog's file names and Arrow schema come from Nautilus internals (`_timestamps_to_filename`). An upgrade from 1.231.0 must run the replay test again.

**Neutral**:
- Measured before this spec: 0.75 GB/h on average and about 2 GB/h at peak of raw feather, 99% book deltas. Milestone 0 saw zstd Parquet at about 3% of the feather bytes. Milestone 1 measured it on 2026-10-08 (06:50 to 09:12 UTC, 3.87 GB of feather from the laptop): book deltas take 616 bytes per row in feather and 16.8 in Parquet (2.73%), trades 504 and 33.5 (6.64%). Deltas are 99.6% of the bytes, so the ratio is about 0.0275, and at the measured 0.75 GB/h average that is about 0.5 GB of Parquet per day (about 1.3 GB on a day at the 2 GB/h peak). `COMPACTOR_PARQUET_RATIO` defaults to 0.042, the ratio × 1.5. The same run hit `data_queue at capacity` in the recorder while the laptop was busy with another test, and the overflow put rows out of order: hour 08:00 overlapped and was kept as feather, as the first AC-12 said. That led to the 2026-10-08 reorder window design (AC-12 to AC-14). The VPS showed no overflow in 72 h, but that was before the compactor shared its 2 CPUs.
- B2 cost grows linearly with compressed bytes; the measured GB per day sets it.
- The backup no longer uploads raw feather, so the nightly upload gets smaller.

## Follow-up

- [x] Milestone 1 writes the measured Parquet GB per day and the compression ratio into Consequences, and updates spec 0001's follow-up "measure the recorder's compressed bytes per day".
- [ ] Feature 12: alert on `catalog_disk_full`, `catalog_retention_blocked`, `catalog_late_rows`, `recorder_queue_full`, `recorder_stop_queue_full`, `compactor_pass_failed`, `backup_failed step=catalog`, and long `book_gap`. Consider a daily count of `catalog_hour_reordered` and `recorder_queue_high`.
- [ ] Feature 15 (simulator): read `gaps/` and skip or split replays at gaps, and treat an `incomplete` hour the same way.
- [ ] A Nautilus upgrade from 1.231.0: check whether `ThrottledEnqueuer` still lets newer messages overtake pending puts, and whether `data_qsize()` still exists.
- [ ] Feature 20 (market context): add its types to `include_types` and to the compactor's type list.
- [ ] Spec 0005: after this ships, run the soak sequence (kfs-soak smoke run, deploy during an open position, 72 h soak).
- [ ] Revisit the 30 GB cap once the measured size per day is known: aim for at least 7 days of local hours.
- [ ] Feature 15 (simulator): a converter from free Tardis days (`incremental_book_L2`, `trades` CSV, downloaded with the `tardis-dev` client) to hourly catalog files, since a whole day (about 45M rows) does not fit in memory at once. A trial on 2026-10-08 loaded and replayed 2M rows of 2026-10-01 with `TardisCSVDataLoader`.
- [ ] `/debug` where the lost messages vanish between the adapter's counter and the stream writer (2026-10-08, BTC and ETH overload; BTC only showed none from 16:27 to 17:00). The `sequence` gaps then show whether a fix worked.
- [ ] Feature 12: alert on `book_sequence_hole`.
- [ ] The 2026-10-08 hours converted before AC-16 ships carry holes that no gap row marks (the 15:00 hour lost 389,870 BTC and ETH messages). Once AC-16 ships, mark those hours `incomplete` by hand, or drop them.
- [ ] Each instrument reload writes about 227 tiny `crypto_perpetual` files into the run folder (Nautilus 1.231.0 keeps a rotation timer per instrument but rotates one shared file). Harmless so far; revisit if the compactor slows on them.
- [ ] Recorder headroom: BTC only still logs `recorder_queue_high` at peaks on the 2 CPU VPS. If restarts come back, raise the heartbeat limit or move to 4 CPUs (`/architect`).

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
