"""Feather to Parquet, one hour at a time, with the book carried across hours (spec 0007).

Rows pass through each stream's reorder window (AC-12), which puts rows that arrived out of
``ts_init`` order back in place and splits off rows later than the window. In order rows go into
the open hour's Parquet writer; book deltas are also applied to a carried
``nautilus_pyo3.OrderBook``. Once the stream's watermark passes the open hour's end, the hour is
finished: Parquet renamed after a row count check, the book saved, late rows merged into
quarantine, then ``done.json`` rewritten.
"""

import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from recorder.compactor.days import write_gaps
from recorder.compactor.store import (
    COMPRESSION,
    TMP_SUFFIX,
    CatalogStore,
    DoneRecord,
    atomic_write,
    merge_quarantine,
    stream_key,
)
from recorder.live_files import FeatherFile
from trading_core.marketdata.errors import HourCountMismatch
from trading_core.marketdata.gaps import Gap, GapDetector
from trading_core.marketdata.hours import (
    HOUR_NS,
    SECOND_NS,
    LateCause,
    WatermarkState,
    cap_watermark,
    end_run,
    hour_of,
    hour_ready,
    hours_inside,
    scan_rows,
)

log = logging.getLogger(__name__)

# The feed writes about 1.5 rows per Arrow batch. Batches are gathered into chunks of at most
# this many rows (65,536 was killed at 512 MB in Milestone 0; 8,192 held 343 MB).
CHUNK_ROWS: Final = 8_192
# Rows per Parquet row group. Fixed, so a file's bytes never depend on how its rows arrived.
ROW_GROUP_ROWS: Final = 8_192
F_LAST: Final = 128
F_SNAPSHOT: Final = 32
# The `action` column value of a CLEAR (the pyo3 stub types `.value` as str; it is an int).
CLEAR: Final[int] = int(p3.BookAction.CLEAR.value)

Beat = Callable[[], None]


def read_chunks(path: Path, beat: Beat | None = None) -> Iterator[Any]:
    """The rows of a feather stream as Arrow tables of about ``CHUNK_ROWS`` rows, in file order.

    A truncated final message (a killed recorder, or a file still being written) ends the file at
    its last complete batch.
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


class HourSink:
    """The Parquet file of one hour being written, under ``<name>.tmp`` until its commit.

    Rows are buffered and written in row groups of exactly ``ROW_GROUP_ROWS`` (the last one
    shorter), so the same rows give the same bytes whatever chunks they came in.
    """

    def __init__(self, path: Path, schema: Any, hour: int) -> None:
        self.path = path
        self.schema = schema
        self.hour = hour
        self._tmp = path.with_name(path.name + TMP_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: Any = pq.ParquetWriter(str(self._tmp), schema, compression=COMPRESSION)
        self._pending: list[Any] = []
        self._pending_rows = 0

    def write(self, table: Any) -> None:
        """Append rows. The schema metadata (instrument and precisions) always comes from the hour."""
        self._pending.append(table.replace_schema_metadata(self.schema.metadata))
        self._pending_rows += table.num_rows
        if self._pending_rows >= ROW_GROUP_ROWS:
            self._drain(final=False)

    def _drain(self, *, final: bool) -> None:
        if not self._pending:
            return
        data = pa.concat_tables(self._pending)
        full = data.num_rows if final else data.num_rows - data.num_rows % ROW_GROUP_ROWS
        for start in range(0, full, ROW_GROUP_ROWS):
            self._writer.write_table(data.slice(start, min(ROW_GROUP_ROWS, full - start)))
        rest = data.slice(full)
        self._pending = [rest] if rest.num_rows else []
        self._pending_rows = rest.num_rows

    def commit(self, expected: int) -> int:
        """Close, check the footer's row count against ``expected``, rename. Returns the file's bytes."""
        self._drain(final=True)
        self._writer.close()
        written = pq.ParquetFile(str(self._tmp)).metadata.num_rows
        if written != expected:
            self._tmp.unlink(missing_ok=True)
            raise HourCountMismatch(self.hour, expected, written)
        os.replace(self._tmp, self.path)
        return self.path.stat().st_size

    def abort(self) -> None:
        """Drop the unfinished file."""
        self._pending = []
        try:
            self._writer.close()
        finally:
            self._tmp.unlink(missing_ok=True)


class BookCarry:
    """The L2 book of one instrument, carried from hour to hour.

    ``valid`` is false until the feed's first ``CLEAR``, and again after a restart, a gap, or a
    late delta; rows that arrive while it is false are written but not applied. ``hour`` is the
    hour whose end the book stands at, once that hour is finished.
    """

    def __init__(self, instrument_id: str) -> None:
        self._id = p3.InstrumentId.from_str(instrument_id)
        self.book = p3.OrderBook(self._id, p3.BookType.L2_MBP)
        self.valid = False
        self.hour: int | None = None

    def invalidate(self) -> None:
        """Forget the book until the feed's next ``CLEAR``."""
        self.book.reset()
        self.valid = False
        self.hour = None

    def apply(self, table: Any) -> None:
        """Apply a chunk of delta rows, starting at the first ``CLEAR`` when the book is invalid."""
        start = 0
        if not self.valid:
            clears = np.flatnonzero(table.column("action").to_numpy() == CLEAR)
            if clears.size == 0:
                return
            start = int(clears[0])
            self.book.reset()
            self.valid = True
        for delta in ArrowSerializer.deserialize(OrderBookDelta, table.slice(start)):
            self.book.apply_delta(delta)

    def restore(self, table: Any, hour: int) -> None:
        """Load a saved book (the snapshot rows written for the start of the next hour)."""
        self.book.reset()
        for delta in ArrowSerializer.deserialize(OrderBookDelta, table):
            self.book.apply_delta(delta)
        self.valid = True
        self.hour = hour

    def snapshot_rows(self, ts: int, schema: Any) -> Any:
        """The book as snapshot deltas at ``ts`` (spec 0007 AC-3).

        One ``CLEAR``, then one ``ADD`` per level (bids best first, then asks best first), all with
        ``order_id`` 0, the last applied ``sequence``, ``ts_event = ts_init = ts``, and
        ``F_SNAPSHOT``; the final row also carries ``F_LAST``.
        """
        meta = schema.metadata
        price_precision = int(meta[b"price_precision"])
        size_precision = int(meta[b"size_precision"])
        sequence = self.book.sequence
        orders = [
            p3.BookOrder(p3.OrderSide.NO_ORDER_SIDE, p3.Price(0, price_precision), p3.Quantity(0, size_precision), 0),
        ]
        actions = [p3.BookAction.CLEAR]
        for level in [*self.book.bids(), *self.book.asks()]:
            first = level.first()
            if first is None:
                continue  # an empty level holds nothing to restore
            orders.append(p3.BookOrder(first.side, first.price, first.size, 0))
            actions.append(p3.BookAction.ADD)
        last = len(orders) - 1
        deltas = [
            p3.OrderBookDelta(self._id, action, order, F_SNAPSHOT | (F_LAST if i == last else 0), sequence, ts, ts)
            for i, (action, order) in enumerate(zip(actions, orders, strict=True))
        ]
        data = p3.book_deltas_to_arrow_record_batch_bytes(deltas)
        table = pa.ipc.open_stream(pa.py_buffer(data)).read_all()
        return table.replace_schema_metadata(meta)


@dataclass(frozen=True)
class LateRows:
    """Rows read below the watermark, with each row's cause and lateness in ns (AC-12)."""

    rows: Any
    causes: list[LateCause]
    lateness_ns: list[int]


class ReorderWindow:
    """One stream's rows read but not yet written, in stable ``ts_init`` order (spec 0007 AC-12).

    ``push`` splits off late rows, holds the rest, and releases the rows at or below the
    watermark. ``moved`` and ``max_late_ns`` collect, per hour, the rows put back in place and the
    largest lateness of a row that was still in the window.
    """

    def __init__(self, key: str, *, window_ns: int, max_rows: int) -> None:
        self.key = key
        self._window_ns = window_ns
        self._max_rows = max_rows
        self.state = WatermarkState()
        self._held: Any | None = None
        self._held_max = 0
        self.moved: dict[int, int] = {}
        self.max_late_ns: dict[int, int] = {}
        self.capped_logged = False

    @property
    def held_rows(self) -> int:
        """Rows held in the window now."""
        return 0 if self._held is None else int(self._held.num_rows)

    def push(self, chunk: Any, run: str) -> tuple[Any | None, LateRows | None]:
        """Take a chunk of ``run`` in file order. Returns the released rows and the late rows."""
        ts = chunk.column("ts_init").to_pylist()
        scan = scan_rows(ts, run, self.state, window_ns=self._window_ns)
        self.state = scan.state
        late = None
        good = chunk
        flags = [cause is not None for cause in scan.causes]
        if any(flags):
            mask = pa.array(flags)
            pairs = [(c, n) for c, n in zip(scan.causes, scan.lateness_ns, strict=True) if c is not None]
            late = LateRows(chunk.filter(mask), [c for c, _ in pairs], [n for _, n in pairs])
            good = chunk.filter(pc.invert(mask))
        if any(scan.lateness_ns):
            for t, n, cause in zip(ts, scan.lateness_ns, scan.causes, strict=True):
                if n and cause is None:
                    hour = hour_of(t)
                    self.max_late_ns[hour] = max(self.max_late_ns.get(hour, 0), n)
        if good.num_rows:
            self._hold(good)
        return self._release(), late

    def end_run(self) -> Any | None:
        """The run has ended: release every held row and raise the watermark past them."""
        self.state = end_run(self.state)
        held, self._held = self._held, None
        return held if held is not None and held.num_rows else None

    def _hold(self, good: Any) -> None:
        ts = good.column("ts_init").to_numpy()
        ordered = bool((ts[1:] >= ts[:-1]).all())
        if self._held is None or self._held.num_rows == 0:
            if ordered:
                self._held, self._held_max = good, int(ts[-1])
                return
            table = good
        elif ordered and int(ts[0]) >= self._held_max:
            self._held, self._held_max = pa.concat_tables([self._held, good]), int(ts[-1])
            return
        else:
            table = pa.concat_tables([self._held, good])  # [held, new]: file order breaks ties
        indices = pc.sort_indices(table, sort_keys=[("ts_init", "ascending")])  # stable
        order = indices.to_numpy()
        moved = order != np.arange(order.size)
        if moved.any():
            table = table.take(indices)
            sorted_ts = table.column("ts_init").to_numpy()[moved]
            hours, counts = np.unique(sorted_ts - sorted_ts % HOUR_NS, return_counts=True)
            for hour, count in zip(hours.tolist(), counts.tolist(), strict=True):
                self.moved[hour] = self.moved.get(hour, 0) + count
        self._held = table
        self._held_max = int(table.column("ts_init")[table.num_rows - 1].as_py())

    def _release(self) -> Any | None:
        held = self._held
        watermark = self.state.watermark
        if held is None or held.num_rows == 0 or watermark is None:
            return None
        ts = held.column("ts_init").to_numpy()
        if held.num_rows > self._max_rows:
            excess = held.num_rows - self._max_rows
            watermark = cap_watermark(ts[:excess].tolist(), held.num_rows, watermark, self._max_rows)
            self.state = replace(self.state, watermark=watermark)
            if not self.capped_logged:
                self.capped_logged = True
                log.warning(
                    "catalog window capped",
                    extra={"fields": {"code": "catalog_window_capped", "stream": self.key, "held_rows": held.num_rows}},
                )
        assert watermark is not None
        n = int(np.searchsorted(ts, watermark, side="right"))
        if n == 0:
            return None
        self._held = held.slice(n)
        return held.slice(0, n)


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
        self._started = False  # whether this process has read a file of the stream yet
        self._newest_hour: dict[str, int] = {}  # fed file -> the newest hour of a row read from it
        self.fed: set[str] = set()
        carried = done.carry.get(instrument_id)
        if carry is not None and carried is not None:
            book = store.book_file(instrument_id, carried)
            if book.exists():
                carry.restore(pq.read_table(str(book)), carried)

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
        self._finish_if_ready(done, now_ns, run_ended=finish)

    def _finish_if_ready(self, done: DoneRecord, now_ns: int, *, run_ended: bool) -> None:
        open_hour = self.open
        if open_hour is None:
            return
        watermark = self.window.state.watermark
        if hour_ready(open_hour.hour, now_ns=now_ns, grace_ns=self._grace_ns, watermark=watermark, run_ended=run_ended):
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
        if self.gaps is not None and self._run is not None:
            clear = (rows.column("action").to_numpy() == CLEAR).tolist()
            self._pending.extend(self.gaps.scan(self._run, ts.tolist(), clear))
        open_hour.sink.write(rows)
        open_hour.source_rows += rows.num_rows
        if self.carry is not None:
            self.carry.apply(rows)

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

    def _commit_gaps(self, done: DoneRecord) -> None:
        """Write the gaps found so far and the detector's state, ahead of the ``done.json`` commit."""
        assert self.gaps is not None
        write_gaps(self._store, self._pending)
        for gap in self._pending:
            log.warning(
                "book gap",
                extra={
                    "fields": {
                        "code": "book_gap",
                        "instrument_id": gap.instrument_id,
                        "kind": gap.kind,
                        "start_ns": gap.start_ns,
                        "end_ns": gap.end_ns,
                        "duration_s": round(gap.duration_s, 3),
                    }
                },
            )
            if gap.kind == "recorder_down":
                done.empty.setdefault(self.instrument_id, set()).update(hours_inside(gap.start_ns, gap.end_ns))
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
            self._commit_gaps(done)
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
