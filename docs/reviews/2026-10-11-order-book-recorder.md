# Review, test/recorder-instruments (order book recorder, spec 0007, PRs #21 to #33), 2026-10-11

**Reviewed by**: Sonnet 5.5 (author on a different model)
**Scope**: 90 files, branch vs 6b28058 (spec 0008 lab files excluded on purpose)
**Verdict**: Changes requested

## Summary

You built a recorder that writes hourly feather files, a compactor that turns them into Parquet with a book snapshot per hour, a sequence watch that finds lost messages, and a disk guard plus catalog backup. The design is careful: `done.json` is one commit point, reruns give the same output, and unknown states fail safe. The core logic is pure and well tested. I found five majors. The most serious one is not in Python: the restore drill's retention rule will, as written, forget your Postgres dump snapshots. I checked that with a throwaway restic repository. The others are about silent data loss paths (a read error treated as a truncated file, no fsync before the feather is deleted, corrupt holes lines skipped silently) and one architecture rule that core now breaks.

## Major

### 🟠 Drill retention forgets the Postgres dump snapshots, `scripts/restore-drill.sh:72`
**Problem**: `RETENTION` uses `--group-by host` with `--keep-daily/weekly/monthly`, plus `--keep-tag catalog`. The catalog snapshot is taken right after the main snapshot, on the same UTC day, so it is the newest of that day. restic gives the daily, weekly and monthly slots to the newest snapshot, and the main snapshot loses its slot. I reproduced this: with a main and a catalog snapshot on each of two days, `restic forget --dry-run` with your exact flags removed the newest main snapshot (`dea518cd`, 2026-10-10) and kept both catalog ones. The reasons column showed the catalog snapshot holding "daily snapshot".
**Why it matters**: Every month the drill's own guard (`recent > 0`) will refuse the prune, so the repository grows without limit. Worse, the day you run `--force-prune` to get past it, the DB dumps older than the daily slots are the ones that go. Your verify note on row 36 saw the guard refuse and treated it as "as designed". It is the symptom of this problem.
**Suggested fix**: Group by host and tags (`--group-by host,tags`). I ran that in the same throwaway repo and the main snapshots (tag `kfs`) kept their daily, weekly and monthly slots. Keep `--keep-tag catalog --keep-tag backlog` so catalog snapshots stay. Add a drill test with a main and a catalog snapshot on one day.

### 🟠 A read error is treated as a truncated file, then the feather is deleted, `apps/recorder/src/recorder/compactor/convert.py:97`
**Problem**: `read_chunks` catches `(pa.ArrowInvalid, OSError)` and just stops, logging at INFO `feather_truncated`. The spec only wants a killed recorder's torn last batch to end a file. An `OSError` (EIO, a flaky disk, a network volume) in the middle of a closed file looks the same. The rows after it are never read, the hour counts match (it only counts what was read), the file goes into `done.files`, and then it is deleted.
**Why it matters**: Silent loss of the rest of a file, with no gap row, no `incomplete` entry and no alert.
**Suggested fix**: Only treat `ArrowInvalid` as a torn tail, and only when the file's run has ended. Let `OSError` propagate (the pass fails, `compactor_pass_failed` alerts). Log a real truncation at WARNING with the bytes left unread. If rows are lost this way mid file, add an `incomplete` entry. Add the "Truncated tail" test the spec lists (I found none).

### 🟠 No fsync before the commit and before the feather is deleted, `packages/core/src/trading_core/catalog/sink.py:19` (also `sink.py:70`, `apps/recorder/src/recorder/compactor/store.py:259`)
**Problem**: `atomic_write`, `HourSink.commit` and `save_done` write, then `os.replace`, with no `fsync` of the file or the folder. The compactor then deletes the feather. Your own spec calls `done.json` "the only commit point".
**Why it matters**: After a host crash or power loss on the VPS, the page cache can lose the renamed Parquet (a new name, so ext4's replace heuristic does not help) or `done.json` while the feather is already gone. The nightly backup would then store the damaged file. The window is small but the loss is permanent.
**Suggested fix**: `fsync` the temp file before the rename, `fsync` the folder after, for Parquet, book files, `done.json`, gaps and manifests. Do it at least before the feather delete. The cost is tiny next to a 3 minute conversion.

### 🟠 Corrupt holes lines are skipped silently, `packages/core/src/trading_core/marketdata/sequence.py:231` (and `apps/recorder/src/recorder/sequence_holes.py:38`)
**Problem**: `parse_holes` counts a complete line that is not valid JSON, or has a bad hole shape, in `bad_lines` and skips it. Nothing ever reads `bad_lines` (grep finds no user). A damaged line can hold a hole or a `checked_to_ns`. On the writing side, `HolesFile.append` ignores the return value of `os.write`, so a short write (disk pressure) leaves a torn line, and the next tick's first line is glued onto it. That makes one bad complete line that swallows a good one.
**Why it matters**: The holes file is the detector of record for lost messages. A dropped hole means a wrong book with no `sequence` gap, which is exactly what AC-17 exists to prevent. It is also the rule "never swallow an error".
**Suggested fix**: Treat any bad complete line as a fail safe hole from the last good `checked_to_ns`, and log ERROR with the count. On the writer, loop until all bytes are written (or write via a buffered file object and check), and put a newline first when the file does not end with one. Add a test for a garbled middle line.

### 🟠 Core now imports Nautilus and does file I/O, against the core rules, `packages/core/src/trading_core/catalog/book.py:7` (also `catalog/layout.py:8`)
**Problem**: `trading_core/catalog` imports `nautilus_trader` (book, layout) and writes Parquet (sink, days). `packages/core/AGENTS.md` says "Only `node/` may import NautilusTrader; everything else in core stays importable on the Intel Mac host." The root rule also says core has no I/O or framework imports. The `__all__` note in `convert.py` says the move was for sharing with the lab (spec 0008).
**Why it matters**: Host side tools and the dashboard can no longer import all of `trading_core` safely, and the next person (or agent) reads AGENTS.md and is misled. If the move was a decision, it is written nowhere in this range.
**Suggested fix**: Either move `catalog/` under `node/` (or a new `trading_core.node.catalog`), or record the exception in `packages/core/AGENTS.md` and the 0007 or 0008 spec, with what stays pure (`marketdata/`) and what does not. The `node` extra also lists `pyarrow` and `numpy` unpinned; pin them like Nautilus or say the lockfile owns it.

## Minor

### 🟡 One failing stream blocks all conversion with no escalation, `apps/recorder/src/recorder/compactor/main.py:132`
**Problem**: Any exception (for example `HourCountMismatch` on one stream) aborts the whole pass, and the next pass hits the same hour again. Trades and every other instrument wait behind it.
**Why it matters**: A single poison hour means no conversion until the recorder reaches the hard floor and stops. The alert exists (`compactor_pass_failed`), but nothing counts repeats.
**Suggested fix**: Keep the fail closed behavior, but log a distinct ERROR with the stream, hour and attempt count after N repeats, so Feature 12 can page on it.

### 🟡 A missing book file restarts the book silently, `apps/recorder/src/recorder/compactor/convert.py:181`
**Problem**: If `done.carry` says hour H is valid but `books/<id>/<H>.parquet` is gone, `restore` is skipped without a log. The carry stays invalid, the next hour starts with no snapshot, and no gap or `incomplete` entry marks it.
**Why it matters**: A replay cannot start at that hour and nothing tells you why.
**Suggested fix**: Log ERROR `catalog_book_missing`, mark that hour `incomplete` with 0, and set `done.carry` to `None`.

### 🟡 The compactor waits on a stalled holes file without saying so, `packages/core/src/trading_core/marketdata/sequence.py:262`
**Problem**: `holes_ready` just returns False while `checked_to_ns` is behind. The recorder's health check does not look at the holes file either, so a stalled 60 s timer keeps the recorder "healthy" while conversion stops and the disk fills.
**Suggested fix**: Log WARNING from the compactor when an otherwise ready hour waits more than a few minutes for `checked_to_ns`, and add the holes file age to `recorder.health`.

### 🟡 Spec 0007 still describes the dropped resubscribe, `docs/specs/0007-order-book-recorder/index.md:75`
**Problem**: verify.md says the resubscribe was dropped (no `RECORDER_RESUBSCRIBE_*` settings, no `recorder_book_resubscribed`). The spec body still has the Resubscribe bullet (line 75), the `resubscribed_ns` rules (line 83), the settings list (lines 285 and 286), the log table rows, the tracker's "asks for one resubscribe" test, and line 95. Status is still "In Progress".
**Suggested fix**: Edit those lines to say it was dropped on the build gate, and set the status when the work is done. (Spec edits belong to `/architect`.)

### 🟡 Tests missing for new branching logic
**Problem**: There is no test for the truncated feather path, a garbled holes line, or `RecordingActor` itself (`on_order_book_deltas`, the timer, `on_stop` writing `closed`). The actor is only checked by the VPS run. The `fetch-catalog.sh` tests are a shell script; fine, but they do not cover `--backlog`.
**Suggested fix**: Test `read_chunks` on a file cut mid batch and on an `OSError`. Drive `RecordingActor` with a fake clock and a fake delta batch if Nautilus lets you; otherwise at least test `SequenceWatch` with a hole across a tick plus a `stop`.

### 🟡 Unused code kept in the tree, `apps/recorder/src/recorder/compactor/bridge.py`
**Problem**: The spec says the offline mode, the bridge and `fetch-catalog.sh --backlog` "stay in the code, unused". That is about 140 lines of bridge, the offline runner, about 100 lines of shell, and their tests, all with a live B2 download cap risk attached.
**Suggested fix**: Delete them (git keeps them) or move them to `scripts/spikes`. If you keep them, mark `--backlog` in the script header as "do not run".

### 🟡 Bare `Any` for Arrow tables, `apps/recorder/src/recorder/compactor/convert.py:79`
**Problem**: `Iterator[Any]`, `rows: Any` and the like appear across `convert.py`, `reorder.py`, `sink.py`, `store.py`. The root rule says no bare `Any`. pyarrow has no stubs, so this is understandable.
**Suggested fix**: One alias, for example `ArrowTable = Any  # pyarrow ships no stubs`, in `trading_core.catalog`, and use it everywhere. That meets the "narrow ignore with a reason" spirit.

### 🟡 Context files are stale
**Problem**: `apps/recorder/AGENTS.md` still says "BTC and ETH", and does not mention the compactor, `health.py`, the disk guard, the sequence watch or the holes file. `packages/core/AGENTS.md` has no `catalog/` or `marketdata/` rows.
**Suggested fix**: Run `/sync` after this review.

## Nits

- ⚪ `packages/core/src/trading_core/marketdata/sequence.py:99`, `_confirmed_first` and `_confirmed_last` grow for the whole run; trivial in size, but a bounded structure would match the "memory stays small" claim.
- ⚪ `apps/recorder/src/recorder/compactor/main.py:204`, `read_bytes` is written to `done.json` but never used to resume; drop it or use it.
- ⚪ `apps/recorder/src/recorder/compactor/convert.py:435`, `book_gap` WARNINGs are logged before the `done.json` commit, so a crash between them logs the same gap twice on the rerun.

## Strengths

- The tracker keeps seen numbers as intervals, so a 189k hole costs almost nothing, and `tick` lowers `checked_to_ns` to the oldest open suspicion. I traced the fill, split and join cases and found them correct.
- `done.json` as the single commit point, with the reorder window state stored per file, makes reruns deterministic. The ready rules (watermark, grace, holes) are pure and tested in core.
- Retention is strict: no valid state, a future cutoff or an unbacked hour means nothing is deleted, whole hours go together, and `quarantine/` files count towards an hour's mtime.
- The fail safe hole for a run that ended without `closed`, and the choice to stop with exit 75 when the holes file cannot be written, both lean the right way for a data recorder.
- `fetch-catalog.sh` passes secrets as `-e NAME` only, uses read only, no lock restic, and resumes file by file.
- The verify.md log is honest: it records the real VPS runs, the wrong "under 2 minutes" row, and the B2 cap incident.

## Test coverage

Core logic is well covered by pure unit tests (`test_marketdata_sequence`, `_gaps`, `_hours`, `_retention`, `_queue_depth`) and the compactor by real file tests (reorder, lifecycle with crash reruns, sequence, retention, backlog, memory, a real hour behind an opt in flag). Disk guard and health are tested with fakes. Gaps: the truncated or unreadable feather path, corrupt holes lines, the `RecordingActor` wiring, a missing carry book file, and the restore drill's retention with a same day main and catalog snapshot. The suite was not run for this review (containers only).
