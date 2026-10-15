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

## 5. Extra laptop checks from the build (AC-5, AC-6) · added 2026-10-15

_Added by `/develop` after the first laptop run. `/check verify` runs these; `/test` can lock the durable ones._

- [ ] During the laptop run, `recorder_flush_slow` shows at most once, in the first minute (the first flush of a busy stream); none after that. → AC-6
- [ ] After `docker compose stop recorder`, the last row of each `order_book_deltas` file is within about 1 s of that file's mtime (rows reach disk right up to the writer's close in `main.py`, even after the actors stop). → AC-5
- [x] `recorder_writer_stats` shows `dropped` 0 on every line. → AC-4

On the first build run (2026-10-15, BTC and ETH): one `recorder_flush_slow` at 347 ms in the first minute, then `max_flush_ms` 66; `busy_pct` 7 to 10; last delta row 83 ms before the file's mtime; `dropped` 0.
