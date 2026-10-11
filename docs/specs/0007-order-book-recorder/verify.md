# Verify: order book recorder · spec 0007 · updated 2026-10-08
_Steps derived from spec 0007 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. This first part covers Milestone 4 (out of order rows); later milestones add their own._

## Commands
- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_marketdata_hours.py tests/test_marketdata_queue_depth.py` → all pass (watermark, late cause, cap, queue levels) → AC-12, AC-13
- [x] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_compactor_reorder.py tests/test_compactor_lifecycle.py tests/test_queue_depth.py` → all pass → AC-2, AC-4, AC-5, AC-11, AC-12, AC-13
- [x] `docker compose run --rm --no-deps -T -e KFS_SLOW_TESTS=1 recorder pytest -q tests/test_compactor_memory.py` → passes under 512 MB with the window full → AC-9, AC-12
- [x] `docker compose config compactor` → command starts `nice -n 19`, `cpu_shares: 256`, `COMPACTOR_REORDER_WINDOW_S` 600, `COMPACTOR_REORDER_MAX_ROWS` 2000000 → AC-14
- [x] On the VPS: `docker inspect` of the compactor shows `CpuShares` 256, `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`, and while the compactor converts a dead run the recorder logs no `recorder_queue_high` → AC-14

## Value sourcing checks
- [x] Watermark: shuffle a stream by up to 590 s → Parquet identical to the sorted stream; delay one group by 700 s → it lands in `quarantine/` with `cause=window` → AC-12
- [x] Late cause: two overlapping runs → late rows carry `cause=run_overlap` → AC-12
- [x] Rows moved and max lateness: `catalog_hour_reordered` shows `rows_moved` > 0 and `max_late_s` under 590 for a shuffled hour → AC-12
- [x] Quarantine path: the late group's file is `quarantine/order_book_deltas/<instrument>/<interval of its hour>.parquet` → AC-12
- [x] Expected rows: the count check passes for an hour with late rows (rows written plus snapshot, late rows excluded) → AC-2
- [x] Window row cap: `COMPACTOR_REORDER_MAX_ROWS=1000` on a shuffled burst logs one `catalog_window_capped` per pass, and every Parquet file keeps `ts_init` non decreasing → AC-12
- [x] Gap state: a pass that ends with rows held gives the same gap file as one long pass, with no `silence` gap → AC-4
- [x] Queue probe and capacity: `build_config` gives `data_engine.qsize` 100000; a fake probe at 50% logs `recorder_queue_high`, at capacity `recorder_queue_full`, each at most once per 60 s → AC-13
- [x] Throttle time: the actor uses `self.clock.timestamp_ns()`; a stop at 99% logs `recorder_stop_queue_full`, at 10% `recorder_stop_queue` → AC-13

## Acceptance criteria coverage (Milestone 4)
- AC-12: reorder, late rows, hour boundary, committed hour, overlap, chunk size, pass boundary, run end, cap, crash rerun
- AC-13: queue watch levels, throttle, stop check, config capacity
- AC-14: compose values now; the VPS step in Milestone 6
- AC-2, AC-4, AC-5, AC-11: count check with late rows, gaps from written rows, manifest late counts, reruns with late rows

## Milestone 5: disk safety and backup (added 2026-10-08)

### Commands
- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_marketdata_retention.py tests/test_settings.py` → all pass (oldest whole hours first, the cutoff, the state check, the floor pair) → AC-6, AC-7
- [x] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_disk.py tests/test_compactor_retention.py` → all pass → AC-6, AC-7
- [x] On the VPS: `docker inspect` of the recorder shows the `python -m recorder.health` health check, and the recorder is healthy → AC-7
- [x] On the VPS: `kfs-compose exec backup kfs-backup once` → a snapshot tagged `kfs` and `catalog` holding `data/`, `quarantine/`, `gaps/`, `manifests/` and no `live/`, `.compactor/`, or `.tmp` (`restic ls`), and `/state/catalog_backup.json` with a cutoff that is not in the future → AC-8
- [x] A backup with Postgres unreachable still runs the catalog step: `backup_failed step=downsample`, then `catalog backup done` and a new state file; `last_success` is unchanged → AC-8
- [x] `scripts/restore-drill.sh` after a catalog snapshot exists → restores the newest main snapshot (never a catalog one) and passes; the retention dry run keeps every `catalog` and `backlog` snapshot → AC-8

### Value sourcing checks
- [x] Catalog bytes: with `CATALOG_CAP_GB` below the size of `data/` and no state file → `catalog_retention_blocked` with `bytes_needed`, nothing deleted → AC-6
- [x] Backed up or not: after a catalog backup, the oldest hours go whole (every table, instrument, and quarantine file), each with `catalog_retention_deleted`, until under the cap; hours written after the cutoff stay → AC-6
- [x] Backup state: a cutoff in the future, an empty `snapshot_id`, or text that is not JSON → `backup_state_invalid`, nothing deleted → AC-6
- [x] Free bytes (retention): with `CATALOG_FREE_FLOOR_GB` above the free space, hours are deleted until the floor holds, even under the cap → AC-6
- [x] Free bytes (recorder): start with `CATALOG_HARD_FLOOR_GB` above the free space (and the free floor above it) → one `catalog_disk_full`, no node, no restarts, unhealthy; restore the default → `catalog_disk_ok` and healthy → AC-7
- [x] Stream freshness: a recorder whose current run stops writing book deltas for 60 s turns unhealthy, even with a fresh heartbeat → AC-7
- [x] Cutoff: the state file's `cutoff` equals the time the catalog step began (the `catalog backup done` log line), not the end of the upload → AC-8

### Acceptance criteria coverage (Milestone 5)
- AC-6: retention unit and file tests; the cap, floor, blocked, and invalid state checks above
- AC-7: guard, wait at start, exit 75, health check tests; the laptop run on 2026-10-08 (wait without restarts, healthy in 11 s); the VPS health step
- AC-8: the backup image run against a local restic repository on 2026-10-08 (main chain failed, catalog step ran, tags and contents correct, state file written); the VPS and drill steps

## Milestone 7: lost messages (added 2026-10-08)

The build gate found that a resubscribe brings no `CLEAR`, so the resubscribe is dropped (AC-15 allows it): no `recorder_book_resubscribed`, no `resubscribed_ns` lines, and no `RECORDER_RESUBSCRIBE_*` settings.

### Commands
- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_marketdata_sequence.py tests/test_marketdata_gaps.py` → all pass (tracker, holes parser, ready gate, release limit, `sequence` gaps, book cut points) → AC-15, AC-16, AC-4
- [x] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_sequence_holes.py tests/test_compactor_sequence.py tests/test_compactor_convert.py` → all pass → AC-15, AC-16, AC-3, AC-5, AC-11
- [x] Real hour (opt in, files copied from the VPS into a git ignored folder, deleted after): `docker compose run --rm --no-deps -T -v "$PWD/data/evidence-15h:/evidence:ro" -e KFS_REAL_HOUR_DIR=/evidence recorder pytest -q -s tests/test_compactor_real_hour.py` → passes; on 2026-10-08 it printed 8 runs, 161 holes, 137 bad samples with the time only rules and none with `sequence` gaps → AC-17
- [x] Laptop run, BTC only, 150 s, then `SIGTERM`: `live/<instance id>/sequence_holes.jsonl` holds `started_ns`, one `checked_to_ns` per minute (the clock minus 600 s), then `closed`; exit 0 → AC-15
- [x] `docker compose config recorder` → `RECORDER_SEQUENCE_CONFIRM_S` 600 → AC-15
- [x] On the VPS after the deploy: the new run folder holds `sequence_holes.jsonl` with a `checked_to_ns` under 2 minutes old, and the compactor logs `sequence_unchecked_run` once per older run → AC-15, AC-16
- [x] On the VPS after 24 h: replay every converted BTC hour of the day after the deploy, sampled at the first row at or after H + k * 60 s (k = 0 to 59) → no crossed or one sided book outside gap spans, the first 1 s after a feed `CLEAR`, the time before the first valid book, and hours with late rows; count `book_sequence_hole` and `book_gap kind=sequence` → AC-17 (2026-10-11, day 2026-10-10 copied from the VPS over SSH: 24 hours, 20.98 M rows, 1,440 samples, 0 crossed or one sided, none excluded; the H + 0 sample is taken once the hour start snapshot is applied. Gaps that day: 8 `reconnect` under 0.5 s and 2 `recorder_down` near 9.7 s, no `sequence` gap; both runs' `sequence_holes.jsonl` (`9518d7d6`, `d64c1331`, covering the whole day) hold 0 holes; container logs only reach back to the 2026-10-10 15:25 UTC restart and show 0 `book_sequence_hole`)

### Value sourcing checks
- [x] Counter and row time: a hole's `first_missing` and `last_missing` match the `sequence` numbers absent from that run's feather, and its `before_ns` and `after_ns` match the `ts_init` of the numbers on either side → AC-15
- [x] Confirm time: a number overtaken by less than 600 s (`RECORDER_SEQUENCE_CONFIRM_S`) writes no hole; one never seen writes a hole only after 600 s on the actor clock → AC-15
- [x] Holes file path: the folder holding `sequence_holes.jsonl` is the same run folder as that run's feather files → AC-15
- [x] Gap start, end, missing: a `sequence` gap starts at the hole's `before_ns`, ends at the instrument's first feed `CLEAR` at or after `after_ns` (not one between the two), and `book_gap` logs the hole's `missing` → AC-16, AC-4
- [x] Book cut: the hour after a gap start has `snapshot_rows` 0, and every hour the gap touches is in `incomplete` with 0 → AC-16, AC-5
- [x] Fail safe hole: kill the recorder (`docker kill`, no clean stop) → its run gets one `sequence` gap from its last `checked_to_ns` to each instrument's last row, with `missing` -1 → AC-16
- [x] Ready gate: an hour of the current run is not converted while its holes file's `checked_to_ns` is below the hour's end → AC-16

### Acceptance criteria coverage (Milestone 7)
- AC-3: the convert test samples at the first row at or after each minute (k = 1 to 59 in the engine, k = 0 on the snapshot rows)
- AC-4: the `sequence` kind and its precedence over `reconnect` and `silence` (core and compactor tests)
- AC-15: tracker and watch tests, the gate (run folder, cost 0.13% of a core), the laptop run, the VPS steps
- AC-16: compactor sequence tests (two instruments, ready gate rerun, fail safe, torn file, unchecked run, crash rerun, cleanup, book cut)
- AC-17: the real 15:00 hour test; the 24 h VPS check after the deploy

## /check verify run, 2026-10-11

_Laptop images rebuilt from `02afbd1`; on the VPS, one `restic ls` and one compactor restart. Verdict PASS once the restore drill ran (row 36, below)._

- Tests: recorder suite 105 pass (2 opt in skips, both run earlier), core market data and settings 127 pass. The value check rows above are ticked from these tests, except where a real run is named here.
- VPS: compactor `cpu_shares: 256`, `cpus: 1`, `nice -n 19` (`kfs-compose config`, the `kfs` user cannot run `docker inspect`); host `cgroup2fs`; no `catalog_late_rows`, `catalog_hour_mismatch`, or recorder restart in the 20 h since the 2026-10-10 15:25 UTC deploy; one `recorder_queue_high` (depth 51,775 of 100,000, 07:51 UTC, not during a conversion). The live run folder holds only `order_book_deltas`, `trade_tick`, `crypto_perpetual`, `config.json`, and `sequence_holes.jsonl` (AC-1). Hour 09:00 of 2026-10-11 became ready at the 11:00 rotation and was converted at 11:05:21 (AC-2; the log's `duration_s` measures from when the hour's file opened, across passes, not the conversion). Days 2026-10-08 to 2026-10-10 closed with manifests (AC-5).
- VPS backup: the 02:30 UTC run wrote the main snapshot, then catalog snapshot `d8a5025b` tagged `kfs` and `catalog`, holding `data/`, `gaps/`, `manifests/` (no `quarantine/` exists yet), no `live/`, `.compactor/`, or `.tmp`; state file cutoff `02:30:07Z`, the time the catalog step began (rows 34, 45).
- Laptop backup with Postgres down and a scratch catalog in a Docker volume: `backup_failed step=downsample`, then `catalog backup done` (snapshot `539de700`, cutoff 13:21:33Z, the step start), a new state file, `last_success` unchanged; the decoy `live/`, `.compactor/`, and `.tmp` files stayed out (row 35). A macOS bind mount gives restic `input/output error`, so use a Docker volume; the 2026-10-08 laptop catalog snapshot lists folders only, the same cause.
- Laptop recorder on a 1.5 GB tmpfs filled to 500 MB free, hard floor 1 GB: one `catalog_disk_full` (`free_bytes` 524288000), no node, 0 restarts, unhealthy; fill removed at 13:16:24, `catalog_disk_ok` at 13:16:35, healthy at 13:16:40 (row 43). The fill file must be written inside the recorder's own container: a tmpfs volume is empty for each new container.
- Real hour 2026-10-10 19:00: zstd, starts with one `CLEAR` and 3,198 snapshot rows, `order_id` 0, `ts_init` = H, the last with `F_SNAPSHOT | F_LAST` (AC-2, AC-3). Row 14: a scratch copy of the shuffle test printed `rows_moved` 15,064 / 15,043 / 4,358 with `max_late_s` under 590.
- Holes file (row 62, first half): `checked_to_ns` 643 s behind the clock, which is the spec's clock minus 600 s plus the 60 s tick; "under 2 minutes old" in the row is wrong. Second half open: the compactor's logs only reach back to the 2026-10-10 restart, so `sequence_unchecked_run` for the older runs was not seen in this run (it was seen 2026-10-09 07:00).
- Row 62, second half (13:30 UTC): after a compactor restart nothing is logged again, which is right: the "once per run" is kept in `done.json`, where the three older runs logged on 2026-10-09 (`3a829b3e`, `76930ce8`, `e5bf6610`) each hold `{"unchecked": true}`. Run folder `fe28417a` has no entry (a leftover of a few KB, already converted).
- AC-10 (13:29 UTC): `scripts/fetch-catalog.sh --into catalog-lab/fetch-check 2026-10-10 2026-10-10` restored 74 files (72 hours, the gap file, the manifest), 350.3 MB, from catalog snapshot `d8a5025b` in 67 s, exit 0; every file matches the VPS copy by sha256. A `ParquetDataCatalog` query returns 13,403 deltas in the first minute of 19:00 and 1,249 trades that hour, and `instruments()` gives `PF_XBTUSD.KRAKEN`. Copy a catalog into a container with `COPYFILE_DISABLE=1 tar`: macOS `tar` adds `._` files whose names the catalog cannot parse.
- Row 36 (13:55 UTC, laptop repository): `scripts/restore-drill.sh --local --allow-stale` (password piped from `secrets/restic_password`; the newest main snapshot there is from 2026-10-05) restored main snapshot `d702becc` although the newest snapshot is catalog `539de700`, and printed PASS. The retention dry run keeps both `catalog` snapshots (`has tags [catalog]`) and would remove only 4 same day `/staging/dump` snapshots from 2026-10-04 and 2026-10-05; the guard refused that prune (4 younger than 15 days), as designed, so nothing was deleted. The monthly drill against B2 (spec 0005) stays the owner's, since it needs the delete capable key.
- This file has no rows for Milestones 1 to 3 and 6; AC-1 to AC-5 and AC-10 were checked above.
