# 0007. Rationale: order book recorder

## Context

The recorder (`apps/recorder`) is still the scaffold from spec 0001. It is a Nautilus node with data clients only. It subscribes to L2 book deltas and trades for `PF_XBTUSD` and `PF_ETHUSD`, and Nautilus's streaming writer saves everything on the message bus as uncompressed feather (Arrow IPC) files. Spec 0001 promised conversion into the Parquet catalog once per closed hour, plus gap detection and retention, and left all three to feature 8. Nothing does them yet.

What was measured on the VPS on 2026-10-05:
- One run ran from 2026-10-03 15:57 UTC to 2026-10-05 13:03 UTC and wrote 33 GB. That is about 0.75 GB/h on average. A one minute sample of the next run grew about 42 MB, which is about 2 GB/h.
- Book deltas are about 99% of the bytes. The BTC delta file alone is 25 GB, and ETH is 9.5 GB. Trades are 160 MB.
- Files never rotate, so one file holds the whole run. With no `include_types` filter, every run also writes about 30 empty files for unrelated types.
- `/srv/kfs` (49 GB) reached 100% on 2026-10-05 and blocked a deploy. The recorder kept reporting healthy, because its health check only proves that the event loop runs.
- The nightly backup sends the whole `/data/catalog` to B2, raw feather included.

The forces at play:
- **The data is costly to replace.** Kraken offers no download of historical L2 deltas. Every lost hour is lost for good, and scalping research needs weeks of it.
- **The box is small.** 4 GB of RAM, the recorder capped at 768 MB, and the trader holding positions on the same host. One hour of BTC deltas is 1 to 2 GB raw, so nothing can load an hour into memory.
- **Replay needs a starting book.** Deltas only change a book. A replay that starts in the middle of a run has no book to change, unless something stored one.
- **B2 is the history.** Spec 0005 made B2 the off site copy, with a VPS key that cannot delete, and a drill retention of 14 daily, 8 weekly, and 24 monthly snapshots. A file that lives on local disk for only a few days appears in few snapshots. That retention would drop it once those snapshots age out.
- **The recorder has no database access** (spec 0002), so gaps cannot go to Postgres from there.

Without a decision, the disk fills again within a day of being cleared, the 72 h soak of spec 0005 cannot run, and the data has holes that nobody can see.

## Options considered

### Option 1: Convert inside the recorder

A timer in the recorder process converts each closed hour with Nautilus's `convert_stream_to_data`, then deletes the feather.

**Pros**:
- No new service. It uses Nautilus's own conversion function.

**Cons**:
- `convert_stream_to_data` loads the data as objects in memory. One hour of BTC deltas does not fit in the recorder's 768 MB.
- A conversion crash or out of memory kill takes the recorder down with it, and so it loses live data, the one thing that cannot be recovered.
- It writes no hour start snapshot, so replay still has to start at the run start.

### Option 2: Hourly feather rotation plus a separate compactor service (chosen)

The recorder only filters and rotates. A separate service streams closed hours batch by batch into Parquet, carries the book across hours to stamp a snapshot at each hour start, detects gaps, writes a daily manifest, and applies retention gated on the backup.

**Pros**:
- Isolation: the compactor can crash, be killed for memory, or be redeployed without touching recording.
- Memory is bounded by one batch plus the carried books.
- Any hour replays on its own, and every hour carries its gaps.
- Retention is tied to the backup, so local deletion never loses history.

**Cons**:
- One more service, and conversion code that depends on Nautilus internals (Arrow schema and file names).
- The carried book is a new piece of state, and it must be right, or every hour starts wrong.

### Option 3: Convert nightly inside the backup container

The nightly backup converts the day's feather to Parquet before uploading.

**Pros**:
- It reuses an existing nightly loop and needs no new service.

**Cons**:
- Up to a day of raw feather (about 20 to 50 GB) sits on the disk, which is the very problem.
- The backup gains write access to the catalog and Nautilus code in a small shell image.
- A slow conversion delays the database backup.

### Option 4: Keep raw feather and grow the disk

Leave the recorder as it is, rotate the files, and buy a bigger OVH volume, deleting old feather after upload.

**Pros**:
- The least code.

**Cons**:
- Raw size grows unbounded in cost, about 20 to 50 GB per day locally and in B2.
- The backtester still needs conversion, and replay still needs a starting book. The work only moves later.
- No gap detection.

## Rationale

The data cannot be replaced, so the design starts from protecting the recording. That rules out Option 1. It puts the most memory hungry step in the same process as the one stream we cannot get back, and Nautilus's built in conversion does not fit an hour in 768 MB anyway. Option 3 and Option 4 leave a day or more of raw feather on a 49 GB disk that already filled within two days. A separate compactor (Option 2) is the boring answer. One more container from the same image, no new technology, and a crash there costs nothing but a retry.

Replay from any hour was your choice, and it decides the snapshot design. The compactor already reads every delta to convert it, so carrying a Nautilus `OrderBook` across hours costs only CPU. An hour starting snapshot then makes each Parquet hour self contained. That also makes local retention safe: deleting old hours never breaks a later one. Runner up: a daily snapshot. It is cheaper to store, but a test of 14:00 would replay from midnight.

Retention is a byte cap and a free space floor, gated on the backup cutoff. Day sizes swing by a factor of 2 or 3 with volatility, so a fixed number of days is not a bound at all. Gating on the backup keeps the rule "local deletion never loses history". If backups stall, retention stops, and the recorder's hard floor stops recording before the disk is full. You chose to keep stored data over new data, and that choice is loud (CRITICAL `catalog_disk_full`, plus an unhealthy container). For B2 to really be the history, the catalog gets its own `catalog` tagged snapshot that the drill keeps forever. Restic deduplication means each hour's bytes are stored once, however many snapshots name it. Runner up: a plain copy into a B2 prefix with rclone, which DuckDB could read straight from B2. It was rejected for now because it adds a tool and a second key with its own rights.

The cross check offered a simpler snapshot design: a recorder actor writes the DataEngine's managed book into the stream at each hour boundary. Hours would then be independent, with no state carried across them. It was rejected because it adds logic to the one process whose data cannot be recovered. A snapshot bug there would sit in the live stream itself. In the compactor, the same bug costs only a rerun before the feather is deleted. The carry's risks are its ordering and its speed, so the build keeps a single commit point and a Milestone 0 speed gate.

Gaps come from three signals the data itself carries: a feed `CLEAR` in the middle of a run (a reconnect), time between runs (recorder down), and silences of 10 s or more. A Kraken sequence number check was left out. The adapter's carrying of Kraken's `seq` is unverified, and a reconnect already shows up as a fresh snapshot. (Reversed 2026-10-08: the counter was measured and holes were found, see *Lost messages* below.) Gaps go to a Parquet file in the catalog, so they are backed up with the data and read by the simulator next to it. Postgres was rejected because the recorder side has no database access by design.

### Out of order rows from a full data queue (2026-10-08)

**What happened.** During the Milestone 1 measurement, the laptop was busy with another test. The recorder logged Nautilus `data_queue at capacity`, and hour 08:00 had rows going backwards in `ts_init`. The first AC-12 kept that hour as feather.

**Why it happens.** In Nautilus 1.231.0, `ThrottledEnqueuer.enqueue` (`live/enqueue.py`) handles a full queue by creating a task that awaits `queue.put(msg)`. When space frees, newer messages enqueued with `put_nowait` can take the slot before the waiting puts run. So messages are reordered, not dropped. The one exception is shutdown, where `cancel_pending_tasks()` drops the waiting puts. `ts_init` is stamped by the adapter before the queue, so it still holds the true arrival order. The default queue holds 100,000 messages. At about 230 to 600 book messages per second, a full queue means the recorder is roughly 3 to 7 minutes behind. The laptop data was gone by the time of this decision, so the incident's real lateness could not be measured.

**Why the first AC-12 was not enough.**
- A kept hour's feather sat in `live/`, which the catalog backup leaves out, so it was neither backed up nor bounded on disk.
- The book carry stayed off until the next feed `CLEAR`, which only comes with a reconnect or a restart, possibly days later.
- A late row for an hour already committed had no clean path at all.

**Options weighed.**
- **Repair plus prevent (chosen).** A 600 s reorder window in the compactor, a queue depth warning in the recorder, and a low cgroup CPU weight for the compactor. Repair is safe because `ts_init` keeps arrival order. Prevention cuts how often repair is needed, and the warning makes lag visible while it happens instead of an hour later. Con: the most code of the options, and every hour finishes 10 minutes later.
- **Prevent only.** CPU priority and a bigger queue, with the first AC-12 kept. It is simple, but any overflow that still happens costs a whole hour and days of snapshots.
- **Repair only.** It fixes the data but gives no live signal that the recorder is falling behind, and lag beyond 600 s becomes quarantine with no warning first.
- **Accept as designed, plus an alert.** The VPS saw no overflow in 72 h, but that was before the compactor shared its 2 CPUs, and the costs above remain.
- **A bigger queue** (for example 300,000). It only delays the overflow, and it costs maybe 100 to 300 MB of Python objects inside the recorder's 768 MB. The reorder window already covers a full default queue.
- **A hard 0.5 CPU cap on the compactor.** It is a limit even when the box is idle. A 2 GB hour would take about 9 minutes, right at AC-2's 10 minute bound, and the backlog would drag. The cgroup weight gives the same priority under contention and keeps a full core when idle.
- **Sorting the whole hour.** An hour is up to about 3M rows, which risks the 512 MB limit. It would also hide a real two run overlap.
- **Keeping an unrepairable hour whole as feather in quarantine.** A committed hour still needs a late rows path, so that means two code paths and a much bigger quarantine. Late rows files are small Parquet, and the hour keeps everything else.
- **Draining the queue before stopping.** It means reaching into Nautilus's shutdown order on a pinned version, and the disk guard's exit has only 20 s. A log line at stop makes the loss visible without that risk.
- **Hourly resubscribe to bring the book back.** It adds logic to the one process whose data cannot be recovered, and a short gap every hour. Quarantine should be rare with the window in place.

**Why `nice` was not enough.** Each Compose service runs in its own cgroup. Between cgroups, the kernel shares CPU by cgroup weight, so the compactor's `nice` only orders work inside its own container. `cpu_shares: 256` gives it a quarter of the default weight against the recorder and the trader.

**Smaller calls.**
- **600 s window:** it covers a full default queue even at quiet rates, and it costs 30 to 60 MB at peak. Runner up: 300 s, which covers a full queue only at busy rates.
- **Quarantine belongs to its hour** for retention and fetch. Runner up: delete quarantine files on their own cutoff, which complicates later merges into the same file.
- **Rerun determinism from the file's starting watermark,** kept in `done.json`. This keeps the "one pass, skip done hours" design. Runner up: resume from the oldest held row's position, which breaks when a later positioned row was already written.
- **Invalidate only the late row's instrument.** Late rows are a per stream event now, not a sign that two recorders ran at once.
- **`catalog_hour_reordered` is a WARNING:** no data is lost, but the recorder lagged.
- **Cross check fixes (Sonnet 5.5, 2026-10-08), applied:** where a rerun starts and why its classification matches the first pass; gap state from written rows; later hours marked incomplete when their snapshot missed a late delta; run end flushes per run; [held, new] sort order with a no sort fast path; a definition for `rows_moved`; late fields in the manifest JSON; the actor's probe closure and clock; a 99% stop threshold; a row cap on the window for bursts; the cgroup v2 check. Accepted as limits rather than fixed: ties at the same nanosecond, clock steps back of more than 600 s (fails safe into quarantine), and identical late rows collapsing in the dedupe. A sequence tiebreak was left out because Kraken's `sequence` is unverified in the adapter.

### Lost messages found by the sequence counter (2026-10-08)

**What happened.** `/check verify` replayed the BTC 15:00 hour of 2026-10-08 (recorded with BTC and ETH, while `kfs-autoheal` was restarting the starved recorder). A bid at 81240 stayed in the book from 15:40:11 on while the asks fell below it, so the book was crossed from 15:40:44 to at least 15:42:44. No delete for that level was ever recorded.

**What the counter showed.** Every delta carries `sequence`. Split at the hour's restarts, the BTC and ETH rows together count up by exactly 1, except in holes. So the counter is one per recorder run: it starts at 1, both instruments share it, and it keeps counting through a websocket reconnect (the 15:09:13 reconnect carries on at 5,604,634). It is the adapter's own count of book messages, so a hole is a message the adapter handled that never reached the feather, a loss inside our process. In that hour, nine run segments held holes of 1 to 189,289 messages (389,870 in all). The biggest ones line up with the `silence` gaps (98,236 missing in the 31 s from 15:40:44), and the feed resumed each time without a `CLEAR`. A hole of 14,222 in 5 s (15:40:11) was under the silence threshold and logged nothing. From 16:27:27 to 17:00, BTC only, 2,882,718 rows stepped by exactly 1 with no hole. So in normal running a hole means real loss, never noise.

**Why the existing design missed it.** Gap detection reads time only. A hole with traffic around it looks like nothing, and a long one looks like a quiet market (`silence`), after which the carried book is trusted again. The book then keeps stale levels until the next feed `CLEAR`, which only comes with a reconnect. The first design left the counter out because the adapter's handling of Kraken's `seq` was unverified; the measurement above settles that.

**Options weighed.**
- **Recorder detects and resubscribes, the compactor marks (chosen).** The recorder sees every instrument's messages in one order, which a shared counter needs, and it is the only process that can ask for a fresh book. The compactor stays the place where gaps and `incomplete` are decided. Con: new code in the recorder's event loop, the process that starved, and a small file handshake between the two services.
- **Mark only, no resubscribe.** Simpler. But one lost delete spoils the book until the next reconnect, which can be hours, and with a healthy BTC only feed reconnects are rare.
- **Detect in the compactor alone.** It reads one stream at a time, so with more than one instrument per run it would need a second read of every delta file merged across instruments, on a 2 CPU box where the compactor is meant to stay light. It also cannot resubscribe.
- **Ignore holes under a threshold.** Fewer flagged hours, but a single lost delete is enough to leave a stale level, so any threshold lets wrong books through.
- **Mark only the instrument whose rows sit around the hole.** The counter is shared, so the lost messages may belong to another instrument; this guesses wrong in exactly the cases that matter.
- **Find and fix the cause first.** Still owed (Follow-up, `/debug`), but detection is needed anyway: whatever the cause, the catalog must never trust a book with a hole in it.

**Smaller calls.**
- **Confirm after 600 s,** the reorder window, so a row the compactor would still put back in place is never called lost; a later arrival is quarantined by AC-12 anyway.
- **Resubscribe after 60 s, at most every 300 s:** overtaken messages arrived up to 45 s late on the VPS, so 60 s rarely resubscribes for nothing, and the spacing bounds the snapshot bursts.
- **A JSON lines file in the run folder** rather than logs or Postgres: the recorder has no database by design, logs are not a data channel, and the file lives and dies with the run's feather.
- **A crash makes a fail safe hole** over the unchecked tail, because holes there may never have been confirmed.
- **The AC-3 test samples the first row after each 60 s,** since real Kraken rows never carry `F_LAST`; the book equality it checks holds at any row.
- **Cross check fixes (Sonnet 5.5, 2026-10-08), applied:** the book cut by `ts_init` against `before_ns` on written rows; the ending `CLEAR` at or after `after_ns`; one shared clock; the fail safe hole per instrument and for a torn file; rerun determinism from `checked_to_ns`; the tracker's start; the exact resubscribe calls, with trades untouched; exit 75 when the holes file fails; one fsync per 60 s tick; `resubscribed_ns` so a resubscribe `CLEAR` is not a false `reconnect`; AC-3 and AC-17 sample points; `missing` kept out of the gap schema. Accepted rather than changed: the confirm time and the reorder window measure lateness differently, but both mismatches fail safe.

## Smaller calls made while writing

- **Rotation mode**: `SCHEDULED_DATES` with a 1 h interval at 00:00 UTC, so files line up with clock hours. Runner up: `INTERVAL`, which rotates relative to process start and would split each clock hour across two files.
- **No sorting** (superseded 2026-10-08, see below): sources of one hour were appended in order of their first `ts_init`, and an overlap was an error. One recorder writes at a time, so an overlap was taken to mean something was wrong. The Milestone 1 run showed that one recorder alone can write rows out of order, so this was replaced by the reorder window.
- **Compression**: zstd (Parquet's default level). It is better than snappy on these repetitive integer columns, and DuckDB and Nautilus both read it. Runner up: snappy, which is faster but larger.
- **Instrument definitions**: one row in every hour, so any single hour or day (after retention, or in a laptop fetch) carries its own definition. Runner up: write only on change. It is smaller, but the definition disappears once its hour is retired.
- **One pass in time order**: a feather stream has no index, so finding a file's last timestamp means reading all of it. The compactor reads each closed file once, from start to end, and closes hours as the stream passes them. Runner up: plan hour by hour, which would read the 25 GB backlog file about 45 times.
- **Closed file rule**: a file is closed when its run is no longer the current run, or a newer sibling exists. Runner up: a 10 minute mtime age, which would wrongly close a quiet but live trade file.
- **Single commit point**: `done.json` is written last for each hour, and the book state is kept per hour. A crash at any step then reruns cleanly, without applying a book twice.
- **Start below the hard floor**: wait and poll instead of exiting, so Docker never crash loops and recording resumes by itself once space returns.
- **Backed up test**: compare file mtime with the backup's start time. Parquet files are immutable after their rename, so a file older than a successful backup's start was in that snapshot. Runner up: a list of files from `restic ls`, which is exact but slow on a growing repository.
- **Compactor network**: none. It only reads and writes the disk. Runner up: the default network, which it never needs.
- **Instrument stream does not gate an hour** (from Milestone 0): the instrument list refreshes every 60 minutes from the recorder's start, so waiting for its file to rotate could hold each hour back by up to an hour. Deltas and trades gate readiness, and the open instrument file is read up to its last complete batch. Runner up: lower `update_instruments_interval_mins`, which adds REST calls and still does not align with the hour.
- **A space blocked dead run holds back newer hours** (2026-10-08): conversion stays in strict time order. Runner up: keep converting the live run, which needs the hour both runs share to wait in feather and be rebuilt from both sources later.
- **Backlog from B2**: the disk filled on 2026-10-06, before the compactor existed, and recording stopped. The backlog was deleted on the VPS once B2 was shown to hold it, and it converts once on the laptop. Runner up: keep it on the VPS for the compactor. That would have kept recording stopped for days, and recording is the one thing that cannot be redone.
- **Current run from file names** (from Milestone 2, 2026-10-08): the current run is the one with the newest `<ns>` in its feather file names. The folder `st_ctime` was the first choice, but deleting a converted file in a dead run updates that folder's ctime, so a dead run could look current and its files would never count as closed. File names never change after the writer creates them.
- **Laptop key**: a separate read only B2 key, so the laptop can restore without holding the VPS key or the delete capable key.
