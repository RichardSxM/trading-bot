"""Feather to Parquet, one hour at a time, with the book carried across hours (spec 0007).

Rows pass through each stream's reorder window (AC-12), which puts rows that arrived out of
``ts_init`` order back in place and splits off rows later than the window. In order rows go into
the open hour's Parquet writer; book deltas are also applied to a carried
``nautilus_pyo3.OrderBook``. Once the stream's watermark passes the open hour's end, the hour is
finished: Parquet renamed after a row count check, the book saved, late rows merged into
quarantine, then ``done.json`` rewritten.

Each run's holes file (AC-16) holds back rows past its ``checked_to_ns`` and the hours they are in,
and every hole cuts the carried book until the feed's next ``CLEAR`` at or after the hole.
"""

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from recorder.compactor.days import write_gaps
from recorder.compactor.store import (
    COMPRESSION,
    CatalogStore,
    DoneRecord,
    atomic_write,
    merge_quarantine,
    stream_key,
)
from recorder.live_files import FeatherFile
from trading_core.catalog.book import CLEAR, F_LAST, F_SNAPSHOT, BookCarry
from trading_core.catalog.reorder import LateRows, ReorderWindow
from trading_core.catalog.sink import ROW_GROUP_ROWS, HourSink
from trading_core.marketdata.errors import FeatherUnreadable, HourCountMismatch
from trading_core.marketdata.gaps import Gap, GapDetector
from trading_core.marketdata.hours import (
    HOUR_NS,
    SECOND_NS,
    LateCause,
    WatermarkState,
    hour_of,
    hour_ready,
    hours_inside,
)
from trading_core.marketdata.sequence import RunHoles, holes_ready, release_limit

# The book carry, hour sink, and reorder window live in `trading_core.catalog`, shared with the
# lab (spec 0008); they are named here too, where the recorder's code and tests reach them.
__all__ = [
    "CHUNK_ROWS",
    "CLEAR",
    "F_LAST",
    "F_SNAPSHOT",
    "ROW_GROUP_ROWS",
    "Beat",
    "BookCarry",
    "HourSink",
    "HourStream",
    "LateRows",
    "ReorderWindow",
    "atomic_write",
    "read_chunks",
]

log = logging.getLogger(__name__)

# The feed writes about 1.5 rows per Arrow batch. Batches are gathered into chunks of at most
# this many rows (65,536 was killed at 512 MB in Milestone 0; 8,192 held 343 MB).
CHUNK_ROWS: Final = 8_192

Beat = Callable[[], None]


def read_chunks(path: Path, beat: Beat | None = None) -> Iterator[Any]:
    """The rows of a feather stream as Arrow tables of about ``CHUNK_ROWS`` rows, in file order.

    A truncated final message (a killed recorder, or a file still being written) ends the file at
    its last complete batch. pyarrow raises either ``ArrowInvalid`` or ``OSError`` for it, with the
    file read to its end; a failure before the end is a read error or damage, and raises
    ``FeatherUnreadable`` so the pass fails instead of dropping the rest of the file.
    """
    with pa.OSFile(str(path), "rb") as source:
        try:
            reader = pa.ipc.open_stream(source)
        except pa.ArrowInvalid:
            return  # created but nothing written yet
        pending: list[Any] = []
        rows = 0
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                break
            except (pa.ArrowInvalid, OSError) as exc:
                position, size = source.tell(), source.size()
                if position < size:
                    raise FeatherUnreadable(str(path), position, size, str(exc)) from exc
                log.info(
                    "feather file ends in a truncated batch",
                    extra={"fields": {"code": "feather_truncated", "path": str(path), "error": str(exc)}},
                )
                break
            if batch.num_rows:
                pending.append(batch)
                rows += batch.num_rows
            if rows >= CHUNK_ROWS:
                yield _contiguous(pending, reader.schema)
                pending, rows = [], 0
                if beat is not None:
                    beat()
        if pending:
            yield _contiguous(pending, reader.schema)


def _contiguous(batches: list[Any], schema: Any) -> Any:
    """One table with one chunk. The feed's batches hold about 1.5 rows each, and every batch has
    its own buffers, so the 600 s the reorder window holds would cost hundreds of MB as is."""
    return pa.Table.from_batches(batches, schema=schema).combine_chunks()


@dataclass
class _OpenHour:
    hour: int
    sink: HourSink
    started: float
    source_rows: int = 0
    snapshot_rows: int = 0


@dataclass
class _LateLog:
    rows: int = 0
    max_late_ns: int = 0
    cause: LateCause = "window"


class HourStream:
    """One stream (book deltas or trades) of one instrument, converted in one pass over its files.

    ``carry`` is the book for a delta stream and ``None`` for trades; ``gaps`` likewise detects a
    delta stream's gaps (AC-4) from the rows the window writes, and they are committed with the
    hour that holds their end. Late rows go to quarantine (AC-12).
    """

    def __init__(
        self,
        store: CatalogStore,
        table: str,
        instrument_id: str,
        done: DoneRecord,
        *,
        carry: BookCarry | None,
        gaps: GapDetector | None = None,
        grace_ns: int = 0,
        window_ns: int = 600 * SECOND_NS,
        max_rows: int = 2_000_000,
        beat: Beat | None = None,
    ) -> None:
        self._store = store
        self.table = table
        self.instrument_id = instrument_id
        self.key = stream_key(table, instrument_id)
        self.carry = carry
        self.gaps = gaps
        self._grace_ns = grace_ns
        self.window = ReorderWindow(self.key, window_ns=window_ns, max_rows=max_rows)
        self._pending: list[Gap] = []
        self._late: dict[int, list[Any]] = {}  # hour -> late rows not yet merged into quarantine
        self._late_snapshots: set[int] = set()  # hours whose snapshot missed a late delta
        self._late_log: dict[int, _LateLog] = {}  # this pass, per hour
        self._beat = beat
        self.open: _OpenHour | None = None
        self._run: str | None = None
        self._holes: Mapping[str, RunHoles | None] = {}
        self._current: str | None = None
        self._started = False  # whether this process has read a file of the stream yet
        self._newest_hour: dict[str, int] = {}  # fed file -> the newest hour of a row read from it
        self.fed: set[str] = set()
        carried = done.carry.get(instrument_id)
        if carry is not None and carried is not None:
            book = store.book_file(instrument_id, carried)
            if book.exists():
                carry.restore(pq.read_table(str(book)), carried)

    def set_holes(self, holes: Mapping[str, RunHoles | None], current: str | None) -> None:
        """This pass's holes files per run, and the run the recorder writes now (AC-16)."""
        self._holes = holes
        self._current = current
        self._set_limit()

    def _run_holes(self) -> RunHoles | None:
        return None if self._run is None else self._holes.get(self._run)

    def _run_over(self) -> bool:
        return self._run is not None and self._run != self._current

    def _set_limit(self) -> None:
        self.window.limit = release_limit(self._run_holes(), run_ended=self._run_over())

    def drain(self, done: DoneRecord, now_ns: int) -> None:
        """Write the held rows a newer ``checked_to_ns`` lets go, and finish an hour now ready.

        A file is read once, so without this a run's last read file would keep its rows held until
        the recorder's next rotation (AC-16).
        """
        released = self.window.release()
        if released is not None:
            self._route(released, done)
        self._finish_if_ready(done, now_ns, run_ended=False)

    def feed(self, file: FeatherFile, done: DoneRecord, now_ns: int) -> None:
        """Read one closed file from start to end, finishing every hour the watermark passes."""
        rel = self._store.relative(file.path)
        if rel in done.files:
            self.fed.add(rel)  # converted before a restart; nothing in it is left to read
        if rel in self.fed:
            return
        if self._run is not None and file.run != self._run:
            # The next run may hold more of the open hour, so it stays open.
            self._end_run(done, now_ns, next_file=rel, finish=False)
            if self.carry is not None:
                # A restart: the old book no longer matches the feed until its next CLEAR.
                self.carry.invalidate()
        elif not self._started:
            # A rerun starts here, with the window state this file began with (AC-11, AC-12).
            stored = done.watermarks.get(rel) or done.watermarks.get(self.key)
            if stored is not None:
                self.window.state = WatermarkState.from_dict(stored)
        self._run = file.run
        self._started = True
        self._set_limit()
        done.watermarks.setdefault(rel, self.window.state.to_dict())
        for chunk in read_chunks(file.path, self._beat):
            newest = hour_of(int(pc.max(chunk.column("ts_init")).as_py()))
            self._newest_hour[rel] = max(self._newest_hour.get(rel, newest), newest)
            released, late = self.window.push(chunk, file.run)
            if late is not None:
                self._take_late(late, done)
            if released is not None:
                self._route(released, done)
            self._finish_if_ready(done, now_ns, run_ended=False)
        self.fed.add(rel)

    def end_run_if_over(self, files: list[FeatherFile], current: str | None, done: DoneRecord, now_ns: int) -> None:
        """Once the run read last is not the current one and all its files are read, flush it.

        The open hour then finishes too, unless an unread file (of a newer run) may hold more of it.
        """
        run = self._run
        if run is None or run == current:
            return
        unread = [f for f in files if self._store.relative(f.path) not in self.fed]
        if any(f.run == run for f in unread):
            return
        open_hour = self.open
        more = open_hour is not None and any(f.created_ns < open_hour.hour + HOUR_NS for f in unread)
        self._end_run(done, now_ns, next_file=None, finish=not more)

    def end_pass(self, done: DoneRecord) -> bool:
        """Merge late rows still pending, mark read files finished, and log this pass's late rows.

        Returns whether ``done`` changed and needs saving.
        """
        changed = self._flush_late(done) | self._mark_finished(done)
        for hour, entry in sorted(self._late_log.items()):
            log.error(
                "catalog late rows",
                extra={
                    "fields": {
                        "code": "catalog_late_rows",
                        "stream": self.key,
                        "hour_ns": hour,
                        "rows": entry.rows,
                        "max_late_s": round(entry.max_late_ns / SECOND_NS, 3),
                        "cause": entry.cause,
                    }
                },
            )
        self._late_log = {}
        self.window.capped_logged = False
        return changed

    def abort(self) -> None:
        """Drop the open hour after a failed pass; the next pass starts again from ``done.json``."""
        self._pending = []
        self._late = {}
        self._late_snapshots = set()
        if self.open is not None:
            self.open.sink.abort()
            self.open = None

    def _end_run(self, done: DoneRecord, now_ns: int, *, next_file: str | None, finish: bool) -> None:
        """Write every held row of the ended run; ``finish`` also finishes the open hour."""
        held = self.window.end_run()
        state = self.window.state.to_dict()
        # Recorded before the flushed rows are committed, so a rerun of the next file starts
        # from the same state even when the process dies during that commit.
        done.watermarks[self.key] = state
        if next_file is not None:
            done.watermarks.setdefault(next_file, state)
        if held is not None:
            self._route(held, done)
        if self.gaps is not None:
            self._pending.extend(self.gaps.end_run())  # a sequence gap ends at the run's last row
        self._finish_if_ready(done, now_ns, run_ended=finish)

    def _finish_if_ready(self, done: DoneRecord, now_ns: int, *, run_ended: bool) -> None:
        open_hour = self.open
        if open_hour is None:
            return
        watermark = self.window.state.watermark
        if not hour_ready(
            open_hour.hour, now_ns=now_ns, grace_ns=self._grace_ns, watermark=watermark, run_ended=run_ended
        ):
            return
        if holes_ready(open_hour.hour, self._run_holes(), run_ended=run_ended or self._run_over()):
            self._finish(done)

    def _route(self, rows: Any, done: DoneRecord) -> None:
        """Split released rows (in ``ts_init`` order) by hour and write them; done hours are skipped.

        A release can be the whole window (a run end, or the row cap), so rows are written at most
        ``CHUNK_ROWS`` at a time: the book turns every row it applies into a Python object.
        """
        ts = rows.column("ts_init").to_numpy()
        hours = ts - ts % HOUR_NS
        cuts = (np.flatnonzero(np.diff(hours)) + 1).tolist()
        for start, end in zip([0, *cuts], [*cuts, len(ts)], strict=True):
            hour = int(hours[start])
            if done.is_done(self.key, hour):
                continue  # finished before a restart; the carried book already holds it
            for part in range(start, end, CHUNK_ROWS):
                stop = min(part + CHUNK_ROWS, end)
                self._write(hour, rows.slice(part, stop - part), ts[part:stop], done)

    def _take_late(self, late: LateRows, done: DoneRecord) -> None:
        """Hold late rows for quarantine; a late delta also invalidates this instrument's book."""
        ts = late.rows.column("ts_init").to_numpy()
        hours = ts - ts % HOUR_NS
        cuts = (np.flatnonzero(np.diff(hours)) + 1).tolist()
        for start, end in zip([0, *cuts], [*cuts, len(ts)], strict=True):
            hour = int(hours[start])
            self._late.setdefault(hour, []).append(late.rows.slice(start, end - start))
            entry = self._late_log.setdefault(hour, _LateLog())
            entry.rows += end - start
            entry.max_late_ns = max(entry.max_late_ns, max(late.lateness_ns[start:end]))
            if "run_overlap" in late.causes[start:end]:
                entry.cause = "run_overlap"
        if self.carry is not None:
            # Every later hour up to the open one started from a book that missed this delta.
            first = int(hours.min())
            self._late_snapshots.update(h for h in done.hours.get(self.key, {}) if h > first)
            if self.open is not None and self.open.hour > first:
                self._late_snapshots.add(self.open.hour)
            self.carry.invalidate()

    def _flush_late(self, done: DoneRecord) -> bool:
        """Merge pending late rows into quarantine and record ``incomplete``. True if anything was."""
        if not self._late and not self._late_snapshots:
            return False
        incomplete = done.incomplete.setdefault(self.key, {})
        for hour, parts in sorted(self._late.items()):
            path = self._store.quarantine_file(self.table, self.instrument_id, hour)
            # The row count of the merged file, so finding the same rows again never counts twice.
            incomplete[hour] = merge_quarantine(path, pa.concat_tables(parts))
        for hour in self._late_snapshots:
            incomplete.setdefault(hour, 0)
        self._late = {}
        self._late_snapshots = set()
        return True

    def _mark_finished(self, done: DoneRecord) -> bool:
        """Read files whose rows all lie in finished hours join ``files``, deleted at the pass end."""
        committed = max(done.hours.get(self.key, {}), default=None)
        if committed is None:
            return False
        finished = [f for f, newest in self._newest_hour.items() if f in self.fed and newest <= committed]
        for rel in finished:
            done.files.add(rel)
            done.watermarks.pop(rel, None)
            del self._newest_hour[rel]
        return bool(finished)

    def _write(self, hour: int, rows: Any, ts: Any, done: DoneRecord) -> None:
        if self.open is not None and hour > self.open.hour:
            self._finish(done)
        if self.open is None:
            self._start(hour, rows.schema)
        assert self.open is not None
        open_hour = self.open
        scan = None
        if self.gaps is not None and self._run is not None:
            clear = (rows.column("action").to_numpy() == CLEAR).tolist()
            scan = self.gaps.scan_book(self._run, ts.tolist(), clear, self._run_holes())
            self._pending.extend(scan.gaps)
        open_hour.sink.write(rows)
        open_hour.source_rows += rows.num_rows
        if self.carry is not None:
            self.carry.apply(rows, scan)

    def _start(self, hour: int, schema: Any) -> None:
        sink = HourSink(self._store.data_file(self.table, self.instrument_id, hour), schema, hour)
        self.open = _OpenHour(hour=hour, sink=sink, started=time.monotonic())
        carry = self.carry
        if carry is None:
            return
        if carry.valid and carry.hour == hour - HOUR_NS:
            snapshot = carry.snapshot_rows(hour, schema)
            sink.write(snapshot)
            self.open.snapshot_rows = snapshot.num_rows
        elif carry.hour is not None:
            # Hours without rows lie between: the book is stale until the feed's next CLEAR.
            carry.invalidate()

    def _commit_gaps(self, done: DoneRecord, hour: int) -> None:
        """Write the gaps found so far and the detector's state, ahead of the ``done.json`` commit.

        Every hour a ``sequence`` gap touches is ``incomplete`` (AC-16), including the hour just
        finished while a gap is still open.
        """
        assert self.gaps is not None
        write_gaps(self._store, self._pending)
        spoiled: list[tuple[int, int]] = []
        for gap in self._pending:
            fields: dict[str, object] = {
                "code": "book_gap",
                "instrument_id": gap.instrument_id,
                "kind": gap.kind,
                "start_ns": gap.start_ns,
                "end_ns": gap.end_ns,
                "duration_s": round(gap.duration_s, 3),
            }
            if gap.missing is not None:
                fields["missing"] = gap.missing
            log.warning("book gap", extra={"fields": fields})
            if gap.kind == "recorder_down":
                done.empty.setdefault(self.instrument_id, set()).update(hours_inside(gap.start_ns, gap.end_ns))
            elif gap.kind == "sequence":
                spoiled.append((gap.start_ns, gap.end_ns))
        pending = self.gaps.pending
        if pending is not None:
            spoiled.append((pending.start_ns, hour))
        if spoiled:
            incomplete = done.incomplete.setdefault(self.key, {})
            for start, end in spoiled:
                for spoiled_hour in range(hour_of(start), hour_of(end) + 1, HOUR_NS):
                    incomplete.setdefault(spoiled_hour, 0)
        done.streams[self.instrument_id] = self.gaps.state.to_dict()

    def _finish(self, done: DoneRecord) -> None:
        open_hour, self.open = self.open, None
        assert open_hour is not None
        hour = open_hour.hour
        expected = open_hour.source_rows + open_hour.snapshot_rows
        try:
            size = open_hour.sink.commit(expected)
        except HourCountMismatch as exc:
            # The feather stays (its file never reaches `files`); the pass stops and is retried.
            log.error(
                "catalog hour mismatch",
                extra={
                    "fields": {
                        "code": "catalog_hour_mismatch",
                        "stream": self.key,
                        "hour_ns": hour,
                        "expected": exc.expected,
                        "written": exc.written,
                    }
                },
            )
            raise
        carry = self.carry
        if carry is not None and carry.valid:
            book = carry.snapshot_rows(hour + HOUR_NS, open_hour.sink.schema)
            atomic_write(
                self._store.book_file(self.instrument_id, hour),
                lambda tmp: pq.write_table(book, str(tmp), compression=COMPRESSION),
            )
            carry.hour = hour
        done.hours.setdefault(self.key, {})[hour] = {
            "rows": expected,
            "bytes": size,
            "snapshot_rows": open_hour.snapshot_rows,
        }
        if carry is not None:
            done.carry[self.instrument_id] = hour if carry.valid else None
        self._mark_finished(done)
        if self.gaps is not None:
            self._commit_gaps(done, hour)
        self._flush_late(done)
        self._store.save_done(done)
        self._pending = []
        if carry is not None:
            self._store.prune_books(self.instrument_id)
        log.info(
            "catalog hour converted",
            extra={
                "fields": {
                    "code": "catalog_hour_converted",
                    "stream": self.key,
                    "hour_ns": hour,
                    "rows": expected,
                    "snapshot_rows": open_hour.snapshot_rows,
                    "bytes": size,
                    "duration_s": round(time.monotonic() - open_hour.started, 1),
                }
            },
        )
        self._log_reordered(hour)

    def _log_reordered(self, hour: int) -> None:
        """WARNING ``catalog_hour_reordered`` when rows of the hour were put back in place (AC-12)."""
        moved = self.window.moved.pop(hour, 0)
        max_late = self.window.max_late_ns.pop(hour, 0)
        for stats in (self.window.moved, self.window.max_late_ns):
            for old in [h for h in stats if h < hour]:
                del stats[old]
        if moved:
            log.warning(
                "catalog hour reordered",
                extra={
                    "fields": {
                        "code": "catalog_hour_reordered",
                        "stream": self.key,
                        "hour_ns": hour,
                        "rows_moved": moved,
                        "max_late_s": round(max_late / SECOND_NS, 3),
                    }
                },
            )
