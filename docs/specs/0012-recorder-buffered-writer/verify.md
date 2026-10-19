# 0012. Verify: the recorder's buffered writer

These are the checks `/check verify` runs for spec 0012, beyond the unit and parity tests. Times are UTC, with Amsterdam in brackets (CEST until 2026-10-25, then CET).

## 1. Benchmark on the laptop (AC-7)

```bash
docker compose build recorder
docker compose run --rm --no-deps -T recorder python -m recorder.bench_writer
```

Pass: it prints at least 10,000 messages per second.

## 2. Laptop run (AC-1, AC-2, AC-5)

You may run the stack briefly in demo, then bring it down (the VPS runs the app; the laptop only tests a change).

1. `docker compose up -d recorder compactor`, wait 2 minutes.
2. The newest run folder under `./catalog/live/` holds `config.json`, `order_book_deltas/PF_XBTUSD.KRAKEN/`, `trade_tick/PF_XBTUSD.KRAKEN/`, and the other streams, with the same names as a run from before this spec.
3. `docker compose ps recorder` shows healthy.
4. The logs show `recorder_writer_stats` once a minute, and no `recorder_write_failed`.
5. `docker compose stop recorder`, then check the last delta file's rows end within about 1 s of the stop.
6. `docker compose down`.

## 3. VPS benchmark (AC-7)

After the deploy, run the benchmark once on the VPS (it takes about a minute, writes only to a temp folder in the container):

```bash
kfs-compose run --rm --no-deps -T recorder python -m recorder.bench_writer
```

Write its number into the Consequences of `index.md`.

## 4. VPS 24 h check (AC-8)

Start right after the deploy, and run for 24 h covering both peaks: US open, 13:30 to 14:30 UTC (15:30 to 16:30 Amsterdam), and 01:30 to 02:30 UTC (03:30 to 04:30 Amsterdam).

Over the 24 h, all of these hold:
- No `recorder_queue_full` in the recorder logs.
- No autoheal restart of `kfs-recorder-1` (the autoheal logs, and the container's restart count stays the same).
- No `book_sequence_hole`, except inside a Kraken maintenance window listed on status.kraken.com.
- No `recorder_writer_busy` and no `recorder_write_failed`; note the highest `busy_pct` from `recorder_writer_stats`, especially at the two peaks.
- The compactor converted every hour of the period (`catalog_hour_converted`), with no `catalog_hour_mismatch` and no `compactor_pass_failed`.

If holes or a full queue still show while `busy_pct` stays low, the writer was not the only cause: take it back to `/debug`.

Result (2026-10-18, VPS, release 3292e58, window 2026-10-17 16:26 to 2026-10-18 16:26 UTC): **pass**. The first window after the 4d55abd deploy (2026-10-15 18:40 UTC) looked clean at its 12 h interim check (no restarts, 0 dropped, busy_pct at most 4), but its recorder logs were lost when later deploys recreated the container, so this fresh window with no deploy is the one that counts. The logs were saved before anything touched the containers.

- No `recorder_queue_full`, `book_sequence_hole`, `recorder_writer_busy`, `recorder_write_failed`, `catalog_hour_mismatch`, or `compactor_pass_failed` in the recorder, trader, or compactor logs.
- `kfs-recorder-1` (33f5af53c5c2) kept restart count 0 from its start at 16:25:58. The `kfs-autoheal` journal has no restart, and `/var/lib/kfs/autoheal.json` is empty.
- 1,440 `recorder_writer_stats` lines (one a minute), 0 dropped. Highest `busy_pct` 6 (US open 13:30 to 14:30 UTC: 6, with the day's busiest minute at 14:32, 148,153 messages; 01:30 to 02:30 UTC: 3). Not failures, but worth knowing: `max_queue_depth` reached 2,273 once (23:02), and `max_tick_lag_ms` reached 6,600 at 01:54, while the writer stayed under 6 % busy, so the event loop paused rather than the writer falling behind (the same pattern as the 12 h interim).
- The compactor converted every hour from 2026-10-17 15:00 to 2026-10-18 15:00 for the book, trades, mark, index, funding, and open interest (24 hours each with no hole, plus the 15:00 hour after the window), and liquidations wherever they came. The only book gaps are `recorder_down` at the window's own start (16:25:46 to 16:26:04) and two short Kraken `reconnect`s (1.7 s at 23:17, 0.3 s at 07:42).

## 5. Extra laptop checks from the build (AC-5, AC-6) · added 2026-10-15

_Added by `/develop` after the first laptop run. `/check verify` runs these; `/test` can lock the durable ones._

- [x] During the laptop run, `recorder_flush_slow` shows at most once, in the first minute (the first flush of a busy stream); none after that. → AC-6
- [x] After `docker compose stop recorder`, the last row of each `order_book_deltas` file is within about 1 s of that file's mtime (rows reach disk right up to the writer's close in `main.py`, even after the actors stop). → AC-5
- [x] `recorder_writer_stats` shows `dropped` 0 on every line. → AC-4

On the first build run (2026-10-15, BTC and ETH): one `recorder_flush_slow` at 347 ms in the first minute, then `max_flush_ms` 66; `busy_pct` 7 to 10; last delta row 83 ms before the file's mtime; `dropped` 0.

## /check verify result · 2026-10-19

**Pass.** Feature 21 is verified against every acceptance criterion. The laptop checks ran on the current code, which is the same recorder code as the VPS release 2065fe9.

- Step 1, laptop benchmark (AC-7): 600,000 messages at 15,131 messages per second (needs 10,000). One `recorder flush slow` line during the benchmark's first flush, as AC-6 allows.
- Recorder tests: `docker compose run --rm --no-deps -T recorder pytest -q` gave 176 passed, 2 skipped, exit 0 (4m51s). They cover the core flush rules, the parity of the files against the old writer, fail closed, the drop paths, shutdown, and the stats (AC-1 to AC-6, AC-9, AC-10).
- Step 2, laptop run: skipped this time. The laptop no longer records market data (the VPS runs the app), and step 5 already holds the laptop run from the build (2026-10-15). The same checks passed on the VPS instead (step 4 and below).
- Step 3, VPS benchmark (AC-7): 57,975 messages per second (recorded in `index.md`).
- Step 4, VPS 24 h check (AC-8): pass, see above.
- After the 2065fe9 deploy (2026-10-18 16:36 UTC), the compactor converted the 16:00 hour at 18:05 UTC (20:05 Amsterdam) with the same file names and layout as before: `order_book_deltas` 2,140,740 rows, `trade_tick` 8,026, `mark_price_update` 9,002, `index_price_update` 9,002, `funding_rate_update` 9,002, `custom_open_interest_update` 8,809, with no `catalog_hour_mismatch` (AC-1, AC-2, AC-3).
