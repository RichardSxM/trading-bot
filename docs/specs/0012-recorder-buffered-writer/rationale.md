# 0012. Rationale: the recorder's buffered writer

## Context

The recorder (spec 0007) is a Nautilus node with data clients only. It records the BTC perp's book deltas, trades, instrument definition, and market context into hourly feather files under `live/<run>/`, which the compactor turns into the Parquet catalog. Since 2026-10-08 it records BTC only, because the 2 CPU VPS could not keep up with BTC and ETH.

Even BTC alone keeps failing at peaks. On 2026-10-15, `/debug` profiled the VPS recorder with py-spy (30 s, `--nonblocking`). The main Python thread sat at 100% of one core, and 87.6% of it was inside Nautilus 1.231.0's `StreamingFeatherWriter.write`: `serialize_batch([obj])` 36.6%, `serialized.nbytes` 23.4%, `write_table` 13.2%. The sequence watch was 0.2%. A benchmark put the writer at about 541 µs per message, a ceiling of about 1,850 messages a second. Serializing 1,500 messages in one batch cost 14.8 µs each, 36 times less.

The cause is in how the kernel wires the writer. `NautilusKernel._setup_streaming` builds one `StreamingFeatherWriter` and subscribes its `write` to every message on the bus (`trader.subscribe("*", writer.write)`). `write` serializes each message alone into a one row Arrow table, adds up its `nbytes`, and writes it as its own record batch. Kraken sends about 1.5 deltas per message, and BTC runs 500 to 1,500 messages a second, peaking at US open and around 02:00 UTC. At the peak the event loop cannot keep up: `data_queue` fills to its 100,000 cap, the adapter's message counter shows holes, the heartbeat stalls, and autoheal restarts the recorder (three times an hour at most).

The constraints: the compactor reads the run folder layout, names, schemas, and metadata exactly as the 1.231.0 writer makes them, and its correctness (reruns, reorder window, gaps) took a long build to prove; NautilusTrader is pinned at 1.231.0 across every app; the VPS has 2 vCPUs shared with the trader and the compactor; and the project's rule is that anything losing data fails loudly. Leaving it means a gap and an incomplete hour at every busy period, which are exactly the hours scalping research wants most.

## Options considered

### Option 1: Our own buffered writer on the bus

A recorder class takes over the writer's job. `main.py` subscribes it to `"*"` with the same public `trader.subscribe` call the kernel uses, and drops `StreamingConfig`. It copies the 1.231.0 routing and file naming, holds each stream's messages, and serializes them in one batch per flush (a 1 s tick, a message cap, rotation, stop). An actor runs the ticks on the node clock.

**Pros**:
- Removes the measured cost at its root: one serialize and one write per stream per second, not per message.
- Uses only public Nautilus APIs (`trader.subscribe`, `ArrowSerializer`, `class_to_filename`, `list_schemas`), so no private kernel attribute is touched.
- Same files on disk, so the compactor, the health check, the backup, and the lab are untouched; a parity test pins it.
- We control the failure path: fail closed instead of log and continue. And the rotation drift and the tiny instrument files get fixed along the way.

**Cons**:
- We own a copy of Nautilus's routing and metadata rules, which an upgrade must check again.
- Up to about 1 s of messages sits in memory, lost on a crash.

### Option 2: Subclass `StreamingFeatherWriter` and swap it into the kernel

Override `write` to buffer and keep the parent's helpers (`_create_identifier_writer`, `_extract_obj_metadata`, rotation). Keep `StreamingConfig`, then after construction unsubscribe the kernel's `writer.write`, set `kernel._writer` to the subclass, and subscribe it.

**Pros**:
- Less new code: file creation, metadata, and rotation are inherited.
- The kernel still closes the writer on dispose.

**Cons**:
- Reaches into private kernel state (`_writer`) and private writer methods; an upgrade can break it silently.
- The kernel has already opened its own files and written `config.json` before the swap, so the run folder starts with the kernel writer's empty files; the swap must clean them up or the compactor sees extra streams.
- Inherits the log and continue error handling unless it is overridden too, at which point most of the class is overridden.

### Option 3: Move the writer to a background thread

Keep the per message writer, but have the bus handler only put messages on a queue that a writer thread drains.

**Pros**:
- The event loop only pays for a queue put.
- Disk stalls no longer block the loop.

**Cons**:
- The cost is Python level serialization, which holds the GIL (the lock that lets only one Python thread run at a time), so a second thread competes with the loop for the same lock and frees little; it only helps once it batches too, which is Option 1 plus a thread.
- A second unbounded or bounded queue, with its own overflow and shutdown order problems, beside the data engine's queue that already overflows.

### Option 4: Upgrade NautilusTrader for a faster writer

Move past 1.231.0, hoping a newer release has a Rust feather writer or a batching one.

**Pros**:
- No writer code of our own.

**Cons**:
- Unverified that any release fixes it.
- Breaks the exact pin across the trader, the recorder, and the lab, and every Nautilus dependent check (catalog file names, queue behaviour, the replay tests) has to run again: far more risk than the problem.

## Rationale

Option 1 attacks the measured bottleneck directly. The profile says the cost is per message serialization and writing, and the benchmark says batching makes it 36 times cheaper. Nothing else in the loop is close (the sequence watch is 0.2%), so a cheaper writer is the fix, not more CPUs or a thread. A 1 s flush keeps the batches large at the peak (about 1,500 messages) while matching the loss bound the old writer's `flush_interval_ms` already implied.

It wins over Option 2 because it touches only public APIs and owns its failure path; Option 2 saves maybe a hundred lines but couples the recorder to private kernel state, and its swap leaves the kernel writer's files in the run folder. Option 3 is ruled out by the GIL: the work is Python level, so a thread does not take it off the loop's core. Option 4 is a large, unproven risk across every app to fix one process.

The cost of Option 1 is the copied routing. That is why the parity test compares the two writers file by file, and why the compactor stays unchanged: if the files are the same, every guarantee spec 0007 proved still holds. The engineer chose to fail closed on write errors, to keep BTC only until the new stats show the headroom, and to roll back with git rather than keep a switch between two writers.
