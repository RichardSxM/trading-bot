# Review, feat/recorder-buffered-writer, 2026-10-15

**Reviewed by**: Claude Sonnet 5.5 (author model not stated)
**Scope**: 25 files, branch vs main (10 commits)
**Verdict**: Approve with nits

## Summary
The change replaces Nautilus's per message feather writer with our own `BufferedFeatherWriter`. It holds messages per stream, writes them once a second, at a cap, at rotation, on stop and at close, and fails closed on any write error. It also adds a lock to `SequenceWatch` because timer callbacks run off the event loop thread. The design follows spec 0012 closely, the threading is handled carefully, and the tests are broad. I found no blockers or majors. The findings are a few convention gaps and small hardening items.

## Minor
### 🟡 Bare `Any` in the writer's types, `apps/recorder/src/recorder/writer.py:80`
**Problem**: `_Stream.writer` is typed `Any` with a comment saying it is a `pa.RecordBatchStreamWriter`. `_write` (line 172), `_stream_for` (line 195), `_open_instrument` and `_metadata` also take `actual: Any`.
**Why it matters**: The project rule is strict types with no bare `Any`. pyarrow is typed enough for the writer, and `actual` can be a union of the recorded data classes or a narrow Protocol with `instrument_id`.
**Suggested fix**: Type `writer` as `pa.RecordBatchStreamWriter`. Give `actual` a small Protocol or a union. If a stub gap really blocks it, use a narrow `# type: ignore[code]` with a reason, as the rules say.

### 🟡 Some errors in `write` escape the fail closed path, `apps/recorder/src/recorder/writer.py:166`
**Problem**: Lines 166 to 177 (`class_to_filename`, `actual.instrument_id.value`, the `hasattr` check) run before the `try` at line 180. An error there (for example a custom type whose `instrument_id` is unexpected) goes up into the message bus handler. It is not logged as `recorder_write_failed`, does not set `tripped`, and does not stop the recorder.
**Why it matters**: AC-4 says any error while writing fails closed. Today the effect depends on what the bus does with a handler exception, and rows for that message are silently missing.
**Suggested fix**: Move the routing code inside the `try` (compute the table name first for the error log), or wrap the whole `_write` body so every exception reaches `_fail`.

### 🟡 No test that the writer lock really protects against the timer thread, `apps/recorder/tests/test_writer.py`
**Problem**: The spec's thread invariant names two locked objects, the writer and `SequenceWatch`. AC-10 gets four thread tests for `SequenceWatch`. The writer lock (`write` versus `flush_all`, `take_stats`, `close`) has none, and the actor tests drive the callbacks on one thread.
**Why it matters**: The writer lock guards the most data critical state (the held lists and the open files). Removing or narrowing it would pass every current test.
**Suggested fix**: Add one deterministic test like AC-10 test (1): block a flush inside a patched `write_table` on an `Event`, call `write` from a second thread, and check it has not returned until the event is set. A second test could run a tick and a stream of writes with a very low `sys.setswitchinterval` and compare rows with the in order result.

### 🟡 A flush failure at stop calls `stop_process` while the node is already stopping, `apps/recorder/src/recorder/writer.py:360`
**Problem**: `flush_on_stop` uses `_flush_streams()` with `stop=True`. A write error there sends a second SIGTERM and starts the 20 s `os._exit(75)` timer while the normal stop (1 s post stop, up to 10 s client disconnect, dispose, close) is still running. `close` uses `stop=False` for this reason.
**Why it matters**: The outcome is still exit 75 and the lost rows are already lost, but the timer can end the process before `close` finishes closing files, and a second SIGTERM during shutdown is unneeded. I did not confirm how Nautilus treats a repeated SIGTERM, so this is a risk, not a proven fault.
**Suggested fix**: Call `_flush_streams(stop=False)` from `flush_on_stop`, since the process is already shutting down, and rely on `main.py` seeing `writer.tripped`.

## Nits
- ⚪ `apps/recorder/src/recorder/writer.py:157`, the after close check runs before the type filter, so the first unrecorded message the bus delivers after `close` logs `recorder_write_after_close` and counts as dropped. Filter by type first if you want that log to mean a recorded message was lost.
- ⚪ `apps/recorder/src/recorder/writer.py:187`, `len(obj.deltas)` builds the full delta list for every book message just to count rows, and `RecordingActor` and the serializer build it again. Counting at flush time, or caching the count, would trim the hot path. Measure first, since the benchmark already passes with room.
- ⚪ `apps/recorder/src/recorder/writer.py:336`, `start_stats` throws away counters from before the flush actor starts (for example the startup `recorder_write_no_instrument` drops), so the first stats line under counts. Harmless, but worth a comment.
- ⚪ `apps/recorder/src/recorder/bench_writer.py:63`, the synthetic prices and sizes are built from floats (`Price(80_000 + i % 40 * 0.5, 1)`). It is only a benchmark, but it breaks the no floats rule on the letter; `Price.from_str` or `Decimal` avoids that.
- ⚪ `apps/recorder/AGENTS.md`, still says the recorder writes through Nautilus's streaming writer and does not list `writer.py` or `WriterFlushActor`. The spec already lists this for `/sync`.
- ⚪ `apps/recorder/src/recorder/main.py:176`, if `node.run()` raises, the process exits through the exception and `writer.tripped` is never read. That matches how the disk guard works today, so it is fine, just noting it.

## Strengths
- The fail closed design is thorough: first error trips the writer, later writes are counted and dropped, `close` writes nothing after a trip (no torn batch behind a bad one), and `main.py` exits 75 so the restart shows as a `recorder_down` gap.
- `SequenceWatch` follows AC-10 exactly: one non reentrant lock per whole method, one acquire per message through `observe_all`, a closed flag that keeps `closed` as the single last line, and `stop` called only after the lock is released. The four required tests are present, including the deterministic blocking one.
- Shutdown is reasoned through end to end: nested `finally` so `close` runs even if `dispose` raises, `timeout_post_stop` cut to 1 s, and `stop_grace_period: 30s` in compose so Docker does not SIGKILL before `close`.
- Rotation matches 1.231.0 (the crossing message lands in the old file) while fixing the drift and the per instrument `crypto_perpetual` file litter, and a parity test plus a compactor equivalence test pin the on disk layout.
- Pure rules (flush cap, next boundary, busy percent, slow flush throttle) sit in `packages/core` with docstrings and unit tests, and the Nautilus adapter code stays in the recorder.

## Test coverage
Good. Covered: batching in order and at the cap, quiet stream flush, an empty tick writing nothing, rotation and the shared file boundary, write error, close error, no instrument yet, stop and close and write after close, stats and tick lag, slow flush throttle, the flush actor on the node clock, `main.py` closing when `dispose` raises and exiting 75, serialize equivalence per type, parity against `StreamingFeatherWriter`, and the compactor result. The gaps are the writer's own thread test (above), a failure while opening the new file at rotation, and the benchmark itself, which is a script with no test (acceptable). I could not run the tests here (they need the Linux containers), so I read them against the code.
