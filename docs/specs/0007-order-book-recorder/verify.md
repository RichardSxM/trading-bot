# Verify: order book recorder · spec 0007 · updated 2026-10-08
_Steps derived from spec 0007 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones. This first part covers Milestone 4 (out of order rows); later milestones add their own._

## Commands
- [ ] `docker compose run --rm --no-deps -T -w /app/packages/core trader pytest -q tests/test_marketdata_hours.py tests/test_marketdata_queue_depth.py` → all pass (watermark, late cause, cap, queue levels) → AC-12, AC-13
- [ ] `docker compose run --rm --no-deps -T recorder pytest -q tests/test_compactor_reorder.py tests/test_compactor_lifecycle.py tests/test_queue_depth.py` → all pass → AC-2, AC-4, AC-5, AC-11, AC-12, AC-13
- [ ] `docker compose run --rm --no-deps -T -e KFS_SLOW_TESTS=1 recorder pytest -q tests/test_compactor_memory.py` → passes under 512 MB with the window full → AC-9, AC-12
- [ ] `docker compose config compactor` → command starts `nice -n 19`, `cpu_shares: 256`, `COMPACTOR_REORDER_WINDOW_S` 600, `COMPACTOR_REORDER_MAX_ROWS` 2000000 → AC-14
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
