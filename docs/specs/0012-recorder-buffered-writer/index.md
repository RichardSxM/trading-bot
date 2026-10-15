# 0012. Write the recorder's feather files through our own buffered writer

**Date**: 2026-10-15
**Status**: In Progress

## Summary

The recorder falls over at busy hours, because the Nautilus feather writer (the code that saves each market data message to disk) does the full, slow save once for every single message. At BTC's peak of about 1,500 messages a second, that uses up a whole CPU core, the queue fills, messages get lost, and the recorder is restarted. This spec replaces that writer with our own, which collects messages for up to 1 second and saves them in one go, about 36 times cheaper. The files on disk look exactly as before, so the compactor and everything after it stay unchanged. This amends spec [0007](../0007-order-book-recorder/index.md) (its AC-1 writer choice); everything else in 0007 stands.

## Requirements

**User stories**:
- As the trader's owner, I want the recorder to keep up with BTC at its busiest hours, so the data has no holes and the recorder is not restarted every peak.
- As the owner, I want a crash to lose at most about a second of data, and every loss to show as a gap, never silently.
- As the owner, I want to see how much headroom the recorder has, so I can decide later whether ETH fits again.

**Acceptance criteria**:
- **AC-1**: The recorder writes its feather files through `BufferedFeatherWriter` (in `apps/recorder`), not the kernel's `StreamingFeatherWriter`; `build_config` sets no `streaming`. Files go to `<CATALOG_PATH>/live/<node.kernel.instance_id>/`. The writer's constructor creates that folder and writes `config.json` (the node config, as the kernel wrote it), so the holes file (0007 AC-15) and the outages file (spec 0009) still find their folder. For the eight `RECORDED_TYPES`, the routing (which messages go to a per instrument stream and which to a shared table file), the folder and file names, the Arrow schema, the schema metadata, and when files open are the same as Nautilus 1.231.0's `StreamingFeatherWriter`: the shared table file (`crypto_perpetual_<ns>.feather`) opens in the constructor, a per instrument file opens on its stream's first message, and the next file opens right at rotation. Test (the parity test): the same message sequence (deltas for two instruments, trades, instruments, mark, index, funding, both custom context types, and an hour boundary) goes through both writers on one frozen shared `TestClock`. Files are paired per stream (table plus instrument, or shared table) in creation order, with the `<ns>` stripped from the names. Each pair has the same relative folder, the same schema metadata, and the same rows (the file's batches concatenated, compared as pyarrow tables). The `crypto_perpetual` table may have fewer files (AC-3, fix 2), but its rows, concatenated in file order, match.
- **AC-2**: Each stream (one file: a table plus instrument, or a shared table) holds the original objects it received since its last flush, in arrival order (a `CustomData` wrapper is held as the wrapper, not its `.data`). It flushes on the writer's flush tick (every `RECORDER_FLUSH_MS`, 1,000, on the node clock), as soon as it holds `RECORDER_FLUSH_MAX_MESSAGES` (4,096), at rotation (AC-3), on stop, and at close. A tick flushes only streams that hold something. A flush is one `ArrowSerializer.serialize_batch(held, data_cls=cls)`, where `cls` is exactly the class 1.231.0 passes (`OrderBookDeltas` for deltas, `data_type.type` for a wrapper), then one `write_table`, then one flush of the file object. An empty or `None` result writes nothing, as in 1.231.0. A message is never reordered, merged, or dropped by batching: a file holds exactly the rows the per message writer would have written, in the same order. Tests: for every recorded type, `serialize_batch` of a list equals the per object calls concatenated; and after a tick, the delta file's size and mtime have grown.
- **AC-3**: Rotation keeps 0007 AC-1's meaning, with two fixes. Each write reads `clock.utc_now()` and `clock.timestamp_ns()` once and uses those values throughout. A stream rotates on its first write at or after its boundary. That message is held first, then the stream's held messages (it included) flush to the old file, the old file closes, and the new `<name>_<ns>.feather` opens, as 1.231.0 writes the message and then rotates. A quiet stream stays in its file past the boundary until its next write, as today. Fix 1: each file's boundary is set when it opens, as the next whole UTC hour strictly after that write's `now` (no drift by the delay of the rotating write). Fix 2: a shared table file (`crypto_perpetual`) has one boundary per file, not one per instrument, so an instrument reload no longer writes about 227 tiny files.
- **AC-4**: Any error while opening, serializing, writing, flushing, or closing a file fails closed. The writer logs CRITICAL `recorder_write_failed` (with `table`, `instrument_id`, `error`), sets `tripped`, refuses every later write (counting them in `dropped`), and stops the recorder through `stop_process` (a clean `SIGTERM`, `os._exit(75)` after 20 s as a fallback; when the disk guard also trips, two fallback timers exist, which is harmless). `main.py` exits 75 once `writer.tripped`, as it does for the disk guard. Once tripped, `close()` flushes nothing, because a failed write can leave a torn batch and later batches behind it would make the compactor's `read_chunks` fail the file. It closes each file best effort, logging any error, and the rows still held are lost. The restart becomes a `recorder_down` gap in the compactor (0007 AC-4). A message whose per instrument file cannot be created because its instrument is not yet in the cache is dropped as 1.231.0 does, counted in `dropped`, and the stream keeps trying on each later message. It logs WARNING `recorder_write_no_instrument` once per stream until the stream's first success.
- **AC-5**: Shutdown loses nothing that reached the writer. The flush actor's `on_stop` flushes every stream. `main.py` closes the writer after `node.dispose()`, in a nested `try`/`finally`, so it runs even when `dispose` raises: a final flush, then each stream writer and file closed. An error during close logs `recorder_write_failed` and leads to exit 75. A write after close is dropped, counted, and logged once as WARNING `recorder_write_after_close`.
- **AC-6**: Every 60 s on the node clock the writer logs INFO `recorder_writer_stats` with `messages`, `rows` (deltas count their `OrderBookDelta` rows, others count 1), `flushes`, `dropped`, `max_flush_ms`, `busy_pct`, `max_queue_depth`, and `max_tick_lag_ms`. `busy_pct` is the time spent inside `write` and the flushes, as a percent of the actual time since the previous stats tick (the first span starts at the actor's start). `max_queue_depth` is the largest data engine queue depth (`data_qsize()`) read at each flush tick. `max_tick_lag_ms` is the largest delay of a flush tick behind its schedule (the event's actual time minus its scheduled time), a direct read of how busy the whole event loop is. When `busy_pct` is 25 or more it also logs WARNING `recorder_writer_busy` with the same fields; at most once per 60 s, because both come from the same tick. A single flush over 250 ms logs WARNING `recorder_flush_slow` (`table`, `instrument_id`, `messages`, `ms`), at most once per 60 s.
- **AC-7**: A benchmark, `python -m recorder.bench_writer`, run in the recorder container on the laptop, pushes 600,000 synthetic Kraken like messages through `BufferedFeatherWriter.write` and its flush tick into a temp folder. The messages are `OrderBookDeltas` for `PF_XBTUSD.KRAKEN` of 1 or 2 deltas alternating (1.5 on average, as measured), plus one `TradeTick` per 10 delta messages. It prints messages per second; it must sustain at least 10,000 (over 6 times the 1,500 peak). The same command runs once on the VPS (brief, `--no-deps`, a temp folder), and its number is written into Consequences.
- **AC-8**: On the VPS, over the first 24 h after the deploy, covering US open (13:30 to 14:30 UTC, 15:30 to 16:30 Amsterdam) and the 02:00 UTC peak (01:30 to 02:30 UTC, 03:30 to 04:30 Amsterdam): no `recorder_queue_full`, no autoheal restart of the recorder, no `book_sequence_hole` outside a Kraken maintenance window listed on status.kraken.com, no `recorder_writer_busy`, and no `recorder_write_failed`. The compactor converts every hour of the period with no `catalog_hour_mismatch` and no `compactor_pass_failed`.
- **AC-9**: The compactor does not change. Its existing tests pass unchanged, and a run folder written by `BufferedFeatherWriter` converts to the same Parquet hours (same rows, same snapshot) as the same messages written by `StreamingFeatherWriter`.

## Decision

**Chosen option**: Option 1: our own buffered writer, subscribed to the message bus the way the kernel subscribes its writer (see [rationale.md](rationale.md)).

`main.py` builds a `BufferedFeatherWriter` right after constructing the node and subscribes it with `node.trader.subscribe("*", writer.write)`, the public call the kernel itself uses. A small `WriterFlushActor` runs its flush tick and its stats tick on the node clock. `StreamingConfig` leaves the recorder.

**Start order in `main.py`**: `TradingNode(config=config)` → `BufferedFeatherWriter(...)` (creates the run folder, writes `config.json`, opens the shared table file) → `node.trader.subscribe("*", writer.write)` → the actors, the `WriterFlushActor` among them → `node.build()` → `node.run()`. The data clients connect and load instruments before the trader starts the actors, so instrument messages may arrive before the first tick; they are held and flush on the first tick or the cap.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Feature design

### How a message flows

```
data engine ──► msgbus "*" ──► BufferedFeatherWriter.write(obj)
                                 1. filter: type in RECORDED_TYPES, else return
                                 2. route: stream key and data class, as 1.231.0 does
                                 3. hold obj
                                 4. rotation due? flush held → close → open new file
                                    else held == cap? flush
WriterFlushActor (node clock)
  every RECORDER_FLUSH_MS ──► writer.flush_all(queue depth, tick lag)   streams holding rows only
  every 60 s              ──► writer.take_stats() ──► recorder_writer_stats / recorder_writer_busy
main.py finally: node.dispose() ──► finally writer.close() ──► exit 75 if writer.tripped
```

**Routing (copied from 1.231.0 `write`, pinned by the parity test)**: the data class is `obj.data_type.type` for a `CustomData` wrapper, else `type(obj)`. The table is `class_to_filename(cls)`. A table in the per instrument set (`order_book_deltas`, `trade_tick`, `mark_price_update`, `index_price_update`, `funding_rate_update`), or a `custom_` table whose data has `instrument_id`, gets a per instrument stream keyed `(table, instrument_id)`, in `<run>/<table>/<urisafe_identifier(id)>/<urisafe_identifier(id)>_<ns>.feather`, with schema `list_schemas()[OrderBookDelta if cls is OrderBookDeltas else cls]` plus the 1.231.0 metadata (`instrument_id`, and price and size precision from the object). Anything else gets the shared table file `<run>/<table>_<ns>.feather`, schema metadata `{"class": cls.__name__}`. The event id dedupe of 1.231.0 is left out: no recorded type is an event.

### Value sourcing

| Action | Value produced | Source |
|---|---|---|
| write | run folder | `<CATALOG_PATH>/live/<node.kernel.instance_id>`, passed to the writer in `main.py` after the node is constructed |
| write | which types | `RECORDED_TYPES` in `recorder/main.py` (unchanged list) |
| write | stream key, file path, schema, metadata | the routing rule above: `class_to_filename`, `urisafe_identifier`, `list_schemas()`, and the object's `instrument_id` and precisions; the instrument must be in `node.kernel.cache` for a per instrument stream, as in 1.231.0 |
| write | file name time | `node.kernel.clock.timestamp_ns()` when the file opens |
| rotate | next boundary | set when the file opens: the next whole UTC hour strictly after that write's `clock.utc_now()` (core `hours.py` hour math), per stream file |
| write, rotate | now and file name time | read once per write: `clock.utc_now()` and `clock.timestamp_ns()` |
| flush | when | the flush tick (`RECORDER_FLUSH_MS`, actor clock timer), `RECORDER_FLUSH_MAX_MESSAGES`, rotation, stop, close |
| flush | the batch | `ArrowSerializer.serialize_batch(held, data_cls=cls)` |
| stats | messages, rows, flushes, dropped | counters in the writer since the last stats tick; rows = `len(obj.deltas)` for `OrderBookDeltas`, else 1 |
| stats | busy time, max flush | `time.perf_counter_ns()` around `write` and each flush, summed and maxed since the last tick |
| stats | busy_pct and the warn | core rule: busy ns × 100 / span ns, the span being the actor clock time since the previous stats tick (or the actor's start), compared with `WRITER_BUSY_WARN_PCT` (25) |
| stats | max_queue_depth | `node.kernel.data_engine.data_qsize()` read at each flush tick, through the same closure `QueueDepthActor` gets in `main.py` |
| stats | max_tick_lag_ms | the flush tick's `TimeEvent`: `ts_init − ts_event` (actual fire time minus scheduled time) |
| slow flush | the warn | the flush's `perf_counter_ns` time against `FLUSH_SLOW_WARN_MS` (250), throttled on the actor clock to once per 60 s |
| fail | exit code | `EXIT_DISK_FULL` (75) and `stop_process` from `recorder/disk.py` |
| config.json | contents | `config.json()` of the `TradingNodeConfig` that `build_config` returns |

### Code placement

- `packages/core/src/trading_core/marketdata/writer.py` (pure, docstrings, unit tests in core): the flush decision (held count against the cap), the next boundary from `now_ns` (reusing the hour math in `hours.py`), and the stats rule (`busy_pct`, whether to warn), and the slow flush throttle. Constants `WRITER_BUSY_WARN_PCT = 25` and `FLUSH_SLOW_WARN_MS = 250`.
- `apps/recorder/src/recorder/writer.py` (adapter, new): `BufferedFeatherWriter`: routing, held messages per stream, pyarrow stream writers, rotation, `flush_all`, `take_stats`, `close`, `tripped`. It imports Nautilus (serializer, `class_to_filename`), so the health check never imports it.
- `apps/recorder/src/recorder/actor.py`: `WriterFlushActor` (two timers, `on_stop` flushes). Thin, calls the writer.
- `apps/recorder/src/recorder/main.py`: drop `StreamingConfig` and the `pandas` and `RotationMode` imports; follow the start order above; close the writer in a nested `finally` after `node.dispose()`; add `writer.tripped` to the exit 75 check.
- `apps/recorder/src/recorder/bench_writer.py`: the AC-7 benchmark.
- `apps/recorder/src/recorder/settings.py`: the two new settings.
- Tests: `test_stream_rotation.py` is rewritten against `BufferedFeatherWriter` (AC-1, AC-3); a new `test_writer_parity.py` (AC-1, AC-9); a new `test_writer.py` (AC-2, AC-4, AC-5, AC-6).

### Key invariants

- Within a stream, rows are written in arrival order; batching never reorders, merges, or drops a message (0007 AC-12's reorder repair still sees the same order).
- A message held in memory is at most about `RECORDER_FLUSH_MS` old, plus the event loop's delay of the tick; the cap bounds a burst.
- After a write error, nothing more is written by this process, not even at close; the recorder stops and restarts.
- Bus handlers and clock timer callbacks run on the event loop thread (the existing `SequenceWatch` already relies on this; build task 1 checks it). A `threading.Lock` around `write`, the flushes, and `close` still guards the writer's state, because it costs almost nothing.
- The newest `order_book_deltas` file gets new bytes at least every `RECORDER_FLUSH_MS` while deltas flow, so 0007 AC-7's 60 s freshness health check still holds unchanged.
- The run folder layout, names, schemas, and metadata stay what 0007 and its compactor read. The compactor has no code change.
- Still no execution client and no secrets in the recorder.

### Security model

Unchanged from 0007: public market data only, no secrets, no execution client, no new network access.

### Configuration required

Through `RecorderSettings` (both have defaults, so nothing needs adding to `/etc/kfs/kfs.env` before the deploy):
- `RECORDER_FLUSH_MS` (default 1000, from 100 to 5000): the flush tick, which is also the most recorded time a crash can lose.
- `RECORDER_FLUSH_MAX_MESSAGES` (default 4096, from 1 to 100,000): a stream flushes at once when it holds this many messages.

### Logs

| Code | Level | When |
|---|---|---|
| `recorder_writer_stats` | INFO | every 60 s (AC-6) |
| `recorder_writer_busy` | WARNING | `busy_pct` at or above 25 (AC-6) |
| `recorder_write_failed` | CRITICAL | a write error; the recorder exits 75 (AC-4) |
| `recorder_flush_slow` | WARNING | one flush took over 250 ms, at most once per 60 s (AC-6) |
| `recorder_write_no_instrument` | WARNING | a message for an instrument not yet in the cache, once per stream until its first success (AC-4) |
| `recorder_write_after_close` | WARNING | a write after close, once (AC-5) |

### Critical test scenarios

- Parity: one message sequence through both writers on one frozen clock gives the same stream files (paired in creation order, `<ns>` stripped), schemas, metadata, and rows. Verifies **AC-1**.
- Serialize equivalence: for each recorded type, `serialize_batch` of a list equals the per object calls concatenated. Verifies **AC-2**.
- Batching: with a test clock, 10 messages then a tick give one record batch of their rows in order; 4,096 messages flush without a tick; a quiet stream's held rows are on disk after the tick (file size and mtime grew); a tick with nothing held writes nothing. Verifies **AC-2**.
- Rotation: a write after the boundary goes into the old file with the held rows, then the old file closes and a new one opens; the next boundary is the next whole hour, not now + 1 h; two instruments' definitions crossing a boundary give one new `crypto_perpetual` file. Verifies **AC-3**.
- Failure: a `write_table` that raises logs `recorder_write_failed`, calls the injected stop, sets `tripped`, drops later writes, and `close` then flushes nothing. Verifies **AC-4**.
- No instrument yet: messages before the instrument is in the cache are dropped with one warning, and the first message after it is cached opens the stream. Verifies **AC-4**.
- Shutdown: messages after the last tick are on disk after `on_stop` and `close`; `close` runs when `dispose` raises; a write after close is dropped and logged once. Verifies **AC-5**.
- Stats: a known busy time over a 60 s span gives the right `busy_pct`, and 25 warns while 24 does not; a tick event 40 ms late gives `max_tick_lag_ms` 40; a 300 ms flush logs `recorder_flush_slow` once in 60 s. Verifies **AC-6**.
- Compactor: a run folder from the new writer converts to Parquet equal to the old writer's. Verifies **AC-9**.

## Build plan

Tracer Bullet: book deltas first go all the way from the new writer through the compactor to a Parquet hour, then the other types, failure handling, and stats thicken it, and the VPS proves it.

1. Thin thread: core `marketdata/writer.py` (flush rule, next boundary), and `BufferedFeatherWriter` for `order_book_deltas` only, with hold, tick flush, cap, and rotation, in a test that runs a fixture run folder through the compactor's conversion. Also confirm in a live node (a brief laptop run) that bus handlers and clock timer callbacks run on the same thread. Satisfies **AC-2**, **AC-3**, **AC-9**.
2. All eight types with the full routing, the eager shared table file and its single boundary, the serialize equivalence test, and the parity test against `StreamingFeatherWriter`. Rewrite `test_stream_rotation.py`. Satisfies **AC-1**, **AC-2**, **AC-3**, **AC-9**.
3. Wire it into the node: `main.py` (no `StreamingConfig`, the start order, the nested `finally` close, exit 75), `WriterFlushActor`, the two settings. A brief laptop run (demo, then the stack down) confirms files appear under `live/<instance_id>/`, the health check passes, and the compactor converts a closed hour. Satisfies **AC-1**, **AC-2**, **AC-5**.
4. Fail closed and the drop paths (`recorder_write_failed`, `recorder_write_no_instrument`, `recorder_write_after_close`), with the shutdown test. Satisfies **AC-4**, **AC-5**.
5. Stats: the core rule, the 60 s tick, queue depth and tick lag, and the three log codes (`recorder_writer_stats`, `recorder_writer_busy`, `recorder_flush_slow`). Satisfies **AC-6**.
6. The benchmark, run on the laptop (at least 10,000 msg/s). Satisfies **AC-7**.
7. Deploy to the VPS, run the benchmark there once and write its number into Consequences, then the 24 h check in [verify.md](verify.md). Satisfies **AC-7**, **AC-8**.

## Migration plan

**Strategy**: no migration needed, one deploy.
**Phases**:
1. Deploy. The recorder restarts into a new run folder written by the new writer. The run folder from before the restart is a dead run, which the compactor converts as usual (its files came from the old writer, same layout).
**Rollback**: revert the commit and deploy. The old writer writes the same layout, so the compactor never notices.
**Risks**: a routing difference the parity test missed would show as a missing stream or a compactor error on the first hour; the AC-8 check watches for `catalog_hour_mismatch` and `compactor_pass_failed`.

## Consequences

**Positive**:
- Writing costs about 15 µs per message instead of about 541 µs (measured 2026-10-15), so the recorder's thread should have most of its core free at the BTC peak, and the queue, the sequence holes, and the restarts they caused should stop.
- The writer now fails closed. The Nautilus writer logs an error and keeps going, so rows could vanish with no gap; now every loss is a restart and a `recorder_down` gap.
- Rotation no longer drifts, and an instrument reload no longer litters the run folder with tiny `crypto_perpetual` files.
- `recorder_writer_stats` gives a measured headroom number for the ETH decision.

**Negative / tradeoffs**:
- We now own about 200 lines that copy Nautilus 1.231.0's routing and metadata rules. A Nautilus upgrade must run the parity test again; a change in the upstream feather layout would not reach us on its own.
- A crash or kill loses up to about 1 s of messages held in memory (the per message writer lost less, only what sat in the file buffer). The restart's `recorder_down` gap covers it, but the gap starts at the last written row, so those rows are simply not there. The holes file's fail safe hole (0007 AC-16) covers a different span, from its last `checked_to_ns` to each instrument's last written row; together they cover the whole tail.
- Every stream flushes on the same tick, so one tick's cost is their sum. `recorder_flush_slow` shows when that hurts; staggering the streams is left until it is measured.
- Each flush blocks the event loop for one batch (about 20 ms for 1,500 messages); spread over the second, that is cheap, but other handlers on the loop wait during it.
- The benchmark runs on synthetic messages; the real feed's mix may cost a little more. AC-8 is the real check.

**Neutral**:
- Measured before this spec (py-spy, 2026-10-15, 30 s on the VPS): 87.6% of the recorder's main thread was in `StreamingFeatherWriter.write`; `serialize_batch([obj])` 36.6%, `serialized.nbytes` 23.4%, `write_table` 13.2%; the sequence watch was 0.2%.
- The VPS benchmark result goes here after build task 7.

## Follow-up

- [x] Spec 0007: AC-1, the Decision, and the "Recorder headroom" follow up point here.
- [ ] Feature 12: alert on `recorder_write_failed`, `recorder_writer_busy`, and `recorder_flush_slow`.
- [ ] ETH: once AC-8 passes, read a week of `recorder_writer_stats` and decide whether ETH fits on the 2 CPU VPS (disk at about 3.2 GB/h of feather, compactor time, the 30 GB catalog cap). That is a new `/architect` decision.
- [ ] Spec 0009's VPS 24 h checks (steps 5 and 7) expect restarts at peaks until this ships; run them again after AC-8 passes.
- [ ] A Nautilus upgrade from 1.231.0: run the parity test, and check whether `trader.subscribe("*", …)` and `kernel.instance_id` still behave the same.
- [ ] `/sync` updates `apps/recorder/AGENTS.md` (it still says the recorder writes through Nautilus's streaming writer).

## Rationale

Reasoning and options: see [rationale.md](rationale.md).
