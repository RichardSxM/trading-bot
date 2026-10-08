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

Gaps come from three signals the data itself carries: a feed `CLEAR` in the middle of a run (a reconnect), time between runs (recorder down), and silences of 10 s or more. A Kraken sequence number check was left out. The adapter's carrying of Kraken's `seq` is unverified, and a reconnect already shows up as a fresh snapshot. Gaps go to a Parquet file in the catalog, so they are backed up with the data and read by the simulator next to it. Postgres was rejected because the recorder side has no database access by design.

## Smaller calls made while writing

- **Rotation mode**: `SCHEDULED_DATES` with a 1 h interval at 00:00 UTC, so files line up with clock hours. Runner up: `INTERVAL`, which rotates relative to process start and would split each clock hour across two files.
- **No sorting**: sources of one hour are appended in order of their first `ts_init`, and an overlap is an error (AC-12), not something to merge. One recorder writes at a time by design, so an overlap means something is wrong. Runner up: an external merge sort, which is more code for a case that should never happen.
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
