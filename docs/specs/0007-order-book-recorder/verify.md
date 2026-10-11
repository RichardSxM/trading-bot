# Verify: order book recorder · spec 0007 · updated 2026-10-08
_Steps derived from spec 0007 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. This first part covers Milestone 4 (out of order rows); later milestones add their own._

## Commands
- [x] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_marketdata_hours.py tests/test_marketdata_queue_depth.py` → all pass (watermark, late cause, cap, queue levels) → AC-12, AC-13
- [x] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_compactor_reorder.py tests/test_compactor_lifecycle.py tests/test_queue_depth.py` → all pass → AC-2, AC-4, AC-5, AC-11, AC-12, AC-13
- [x] `docker compose run --rm --no-deps -T -e KFS_SLOW_TESTS=1 recorder pytest -q tests/test_compactor_memory.py` → passes under 512 MB with the window full → AC-9, AC-12
- [x] `docker compose config compactor` → command starts `nice -n 19`, `cpu_shares: 256`, `COMPACTOR_REORDER_WINDOW_S` 600, `COMPACTOR_REORDER_MAX_ROWS` 2000000 → AC-14
- [ ] On the VPS: `docker inspect` of the compactor shows `CpuShares` 256, `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`, and while the compactor converts a dead run the recorder logs no `recorder_queue_high` → AC-14

## Value sourcing checks
- [ ] Watermark: shuffle a stream by up to 590 s → Parquet identical to the sorted stream; delay one group by 700 s → it lands in `quarantine/` with `cause=window` → AC-12
- [ ] Late cause: two overlapping runs → late rows carry `cause=run_overlap` → AC-12
- [ ] Rows moved and max lateness: `catalog_hour_reordered` shows `rows_moved` > 0 and `max_late_s` under 590 for a shuffled hour → AC-12
- [ ] Quarantine path: the late group's file is `quarantine/order_book_deltas/<instrument>/<interval of its hour>.parquet` → AC-12
- [ ] Expected rows: the count check passes for an hour with late rows (rows written plus snapshot, late rows excluded) → AC-2
- [ ] Window row cap: `COMPACTOR_REORDER_MAX_ROWS=1000` on a shuffled burst logs one `catalog_window_capped` per pass, and every Parquet file keeps `ts_init` non decreasing → AC-12
- [ ] Gap state: a pass that ends with rows held gives the same gap file as one long pass, with no `silence` gap → AC-4
- [ ] Queue probe and capacity: `build_config` gives `data_engine.qsize` 100000; a fake probe at 50% logs `recorder_queue_high`, at capacity `recorder_queue_full`, each at most once per 60 s → AC-13
- [ ] Throttle time: the actor uses `self.clock.timestamp_ns()`; a stop at 99% logs `recorder_stop_queue_full`, at 10% `recorder_stop_queue` → AC-13

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
- [ ] On the VPS: `kfs-compose exec backup kfs-backup once` → a snapshot tagged `kfs` and `catalog` holding `data/`, `quarantine/`, `gaps/`, `manifests/` and no `live/`, `.compactor/`, or `.tmp` (`restic ls`), and `/state/catalog_backup.json` with a cutoff that is not in the future → AC-8
- [ ] A backup with Postgres unreachable still runs the catalog step: `backup_failed step=downsample`, then `catalog backup done` and a new state file; `last_success` is unchanged → AC-8
- [ ] `scripts/restore-drill.sh` after a catalog snapshot exists → restores the newest main snapshot (never a catalog one) and passes; the retention dry run keeps every `catalog` and `backlog` snapshot → AC-8

### Value sourcing checks
- [ ] Catalog bytes: with `CATALOG_CAP_GB` below the size of `data/` and no state file → `catalog_retention_blocked` with `bytes_needed`, nothing deleted → AC-6
- [ ] Backed up or not: after a catalog backup, the oldest hours go whole (every table, instrument, and quarantine file), each with `catalog_retention_deleted`, until under the cap; hours written after the cutoff stay → AC-6
- [ ] Backup state: a cutoff in the future, an empty `snapshot_id`, or text that is not JSON → `backup_state_invalid`, nothing deleted → AC-6
- [ ] Free bytes (retention): with `CATALOG_FREE_FLOOR_GB` above the free space, hours are deleted until the floor holds, even under the cap → AC-6
- [ ] Free bytes (recorder): start with `CATALOG_HARD_FLOOR_GB` above the free space (and the free floor above it) → one `catalog_disk_full`, no node, no restarts, unhealthy; restore the default → `catalog_disk_ok` and healthy → AC-7
- [ ] Stream freshness: a recorder whose current run stops writing book deltas for 60 s turns unhealthy, even with a fresh heartbeat → AC-7
- [ ] Cutoff: the state file's `cutoff` equals the time the catalog step began (the `catalog backup done` log line), not the end of the upload → AC-8

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
- [ ] `docker compose config recorder` → `RECORDER_SEQUENCE_CONFIRM_S` 600 → AC-15
- [ ] On the VPS after the deploy: the new run folder holds `sequence_holes.jsonl` with a `checked_to_ns` under 2 minutes old, and the compactor logs `sequence_unchecked_run` once per older run → AC-15, AC-16
- [x] On the VPS after 24 h: replay every converted BTC hour of the day after the deploy, sampled at the first row at or after H + k * 60 s (k = 0 to 59) → no crossed or one sided book outside gap spans, the first 1 s after a feed `CLEAR`, the time before the first valid book, and hours with late rows; count `book_sequence_hole` and `book_gap kind=sequence` → AC-17 (2026-10-11, day 2026-10-10 copied from the VPS over SSH: 24 hours, 20.98 M rows, 1,440 samples, 0 crossed or one sided, none excluded; the H + 0 sample is taken once the hour start snapshot is applied. Gaps that day: 8 `reconnect` under 0.5 s and 2 `recorder_down` near 9.7 s, no `sequence` gap; both runs' `sequence_holes.jsonl` (`9518d7d6`, `d64c1331`, covering the whole day) hold 0 holes; container logs only reach back to the 2026-10-10 15:25 UTC restart and show 0 `book_sequence_hole`)

### Value sourcing checks
- [ ] Counter and row time: a hole's `first_missing` and `last_missing` match the `sequence` numbers absent from that run's feather, and its `before_ns` and `after_ns` match the `ts_init` of the numbers on either side → AC-15
- [ ] Confirm time: a number overtaken by less than 600 s (`RECORDER_SEQUENCE_CONFIRM_S`) writes no hole; one never seen writes a hole only after 600 s on the actor clock → AC-15
- [ ] Holes file path: the folder holding `sequence_holes.jsonl` is the same run folder as that run's feather files → AC-15
- [ ] Gap start, end, missing: a `sequence` gap starts at the hole's `before_ns`, ends at the instrument's first feed `CLEAR` at or after `after_ns` (not one between the two), and `book_gap` logs the hole's `missing` → AC-16, AC-4
- [ ] Book cut: the hour after a gap start has `snapshot_rows` 0, and every hour the gap touches is in `incomplete` with 0 → AC-16, AC-5
- [ ] Fail safe hole: kill the recorder (`docker kill`, no clean stop) → its run gets one `sequence` gap from its last `checked_to_ns` to each instrument's last row, with `missing` -1 → AC-16
- [ ] Ready gate: an hour of the current run is not converted while its holes file's `checked_to_ns` is below the hour's end → AC-16

### Acceptance criteria coverage (Milestone 7)
- AC-3: the convert test samples at the first row at or after each minute (k = 1 to 59 in the engine, k = 0 on the snapshot rows)
- AC-4: the `sequence` kind and its precedence over `reconnect` and `silence` (core and compactor tests)
- AC-15: tracker and watch tests, the gate (run folder, cost 0.13% of a core), the laptop run, the VPS steps
- AC-16: compactor sequence tests (two instruments, ready gate rerun, fail safe, torn file, unchecked run, crash rerun, cleanup, book cut)
- AC-17: the real 15:00 hour test; the 24 h VPS check after the deploy
