"""Feather to Parquet, one hour at a time, with the book carried across hours (spec 0007).

Rows go into the open hour's Parquet writer in stream order. For book deltas they are also
applied to a carried ``nautilus_pyo3.OrderBook``. When the first row of a later hour arrives, the
open hour is finished: Parquet renamed after a row count check, the book saved, then
``done.json`` rewritten.
"""

import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from recorder.compactor.days import write_gaps
from recorder.compactor.store import COMPRESSION, TMP_SUFFIX, CatalogStore, DoneRecord, atomic_write, stream_key
from trading_core.marketdata.errors import HourCountMismatch
from trading_core.marketdata.gaps import Gap, GapDetector
from trading_core.marketdata.hours import HOUR_NS, hour_ready, hours_inside

log = logging.getLogger(__name__)

# The feed writes about 1.5 rows per Arrow batch. Batches are gathered into chunks of at most
# this many rows (65,536 was killed at 512 MB in Milestone 0; 8,192 held 343 MB).
CHUNK_ROWS: Final = 8_192
F_LAST: Final = 128
F_SNAPSHOT: Final = 32
# The `action` column value of a CLEAR (the pyo3 stub types `.value` as str; it is an int).
CLEAR: Final[int] = int(p3.BookAction.CLEAR.value)

Beat = Callable[[], None]


@dataclass(frozen=True, order=True)
class FeatherFile:
    """One recorder feather file. Files sort by the creation time in their name."""

    created_ns: int
    path: Path = field(compare=False)
    run: str = field(compare=False)

    @classmethod
    def parse(cls, path: Path, run: str) -> FeatherFile:
        """Read the creation time from ``<name>_<ns>.feather``."""
        return cls(created_ns=int(path.stem.rsplit("_", 1)[1]), path=path, run=run)


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
                yield pa.Table.from_batches(pending, schema=reader.schema)
                pending, rows = [], 0
                if beat is not None:
                    beat()
        if pending:
            yield pa.Table.from_batches(pending, schema=reader.schema)


class HourSink:
    """The Parquet file of one hour being written, under ``<name>.tmp`` until its commit."""

    def __init__(self, path: Path, schema: Any, hour: int) -> None:
        self.path = path
        self.schema = schema
        self.hour = hour
        self._tmp = path.with_name(path.name + TMP_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: Any = pq.ParquetWriter(str(self._tmp), schema, compression=COMPRESSION)

    def write(self, table: Any) -> None:
        """Append rows. The schema metadata (instrument and precisions) always comes from the hour."""
        self._writer.write_table(table.replace_schema_metadata(self.schema.metadata))

    def commit(self, expected: int) -> int:
        """Close, check the footer's row count against ``expected``, rename. Returns the file's bytes."""
        self._writer.close()
        written = pq.ParquetFile(str(self._tmp)).metadata.num_rows
        if written != expected:
            self._tmp.unlink(missing_ok=True)
            raise HourCountMismatch(self.hour, expected, written)
        os.replace(self._tmp, self.path)
        return self.path.stat().st_size

    def abort(self) -> None:
        """Drop the unfinished file."""
        try:
            self._writer.close()
        finally:
            self._tmp.unlink(missing_ok=True)


class BookCarry:
    """The L2 book of one instrument, carried from hour to hour.

    ``valid`` is false until the feed's first ``CLEAR``, and again after a restart, a gap, or an
    overlap; rows that arrive while it is false are written but not applied. ``hour`` is the hour
    whose end the book stands at, once that hour is finished.
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


@dataclass
class _OpenHour:
    hour: int
    sink: HourSink
    started: float
    source_rows: int = 0
    snapshot_rows: int = 0
    last_ts: int = -1
    files: set[str] = field(default_factory=set)


class HourStream:
    """One stream (book deltas or trades) of one instrument, converted in one pass over its files.

    ``carry`` is the book for a delta stream and ``None`` for trades; ``gaps`` likewise detects a
    delta stream's gaps (AC-4), which are written with the hour that holds their end. ``on_overlap``
    is called with the hour when rows go backwards (AC-12), so every carried book can be invalidated.
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
        on_overlap: Callable[[int], None],
        beat: Beat | None = None,
    ) -> None:
        self._store = store
        self.table = table
        self.instrument_id = instrument_id
        self.key = stream_key(table, instrument_id)
        self.carry = carry
        self.gaps = gaps
        self._pending: list[Gap] = []
        self._on_overlap = on_overlap
        self._beat = beat
        self.open: _OpenHour | None = None
        self._run: str | None = None
        self._newest_hour: dict[str, int] = {}  # fed file -> the newest hour it held
        self.fed: set[str] = set()
        carried = done.carry.get(instrument_id)
        if carry is not None and carried is not None:
            book = store.book_file(instrument_id, carried)
            if book.exists():
                carry.restore(pq.read_table(str(book)), carried)

    def feed(self, file: FeatherFile, done: DoneRecord) -> None:
        """Read one closed file from start to end, finishing every hour that the stream passes."""
        rel = self._store.relative(file.path)
        if rel in done.files:
            self.fed.add(rel)  # converted before a restart; nothing in it is left to read
        if rel in self.fed:
            return
        if self._run is not None and file.run != self._run and self.carry is not None:
            # A restart: the old book no longer matches the feed until its next CLEAR.
            self.carry.invalidate()
        self._run = file.run
        for chunk in read_chunks(file.path, self._beat):
            ts = chunk.column("ts_init").to_numpy()
            hours = ts - ts % HOUR_NS
            cuts = (np.flatnonzero(np.diff(hours)) + 1).tolist()
            for start, end in zip([0, *cuts], [*cuts, len(ts)], strict=True):
                hour = int(hours[start])
                self._newest_hour[rel] = max(self._newest_hour.get(rel, hour), hour)
                if hour in done.overlaps.get(self.key, ()):
                    done.kept.add(rel)
                    continue
                if done.is_done(self.key, hour):
                    continue  # finished before a restart; the carried book already holds it
                self._write(hour, chunk.slice(start, end - start), ts[start:end], rel, done)
        self.fed.add(rel)

    def finish_if_passed(self, *, now_ns: int, grace_ns: int, more_source: bool, done: DoneRecord) -> None:
        """Finish the open hour when no file that could hold more of its rows is left to read."""
        if self.open is None or more_source:
            return
        if hour_ready(self.open.hour, now_ns=now_ns, grace_ns=grace_ns, positions={}, run_ended=True):
            self._finish(done)

    def abort(self) -> None:
        """Drop the open hour after a failed pass; the next pass starts again from ``done.json``."""
        self._pending = []
        if self.open is not None:
            self.open.sink.abort()
            self.open = None

    def _write(self, hour: int, rows: Any, ts: Any, rel: str, done: DoneRecord) -> None:
        if self.open is not None and hour > self.open.hour:
            self._finish(done)
        if self.open is None:
            self._start(hour, rows.schema)
        assert self.open is not None
        open_hour = self.open
        if hour < open_hour.hour or int(ts[0]) < open_hour.last_ts or bool((np.diff(ts) < 0).any()):
            self._overlap({open_hour.hour, hour}, rel, done)
            return
        if self.gaps is not None and self._run is not None:
            clear = (rows.column("action").to_numpy() == CLEAR).tolist()
            self._pending.extend(self.gaps.scan(self._run, ts.tolist(), clear))
        open_hour.sink.write(rows)
        open_hour.source_rows += rows.num_rows
        open_hour.last_ts = int(ts[-1])
        open_hour.files.add(rel)
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

    def _overlap(self, hours: set[int], rel: str, done: DoneRecord) -> None:
        """AC-12: write nothing for the hour, keep its feather, and invalidate every carried book."""
        open_hour, self.open = self.open, None
        assert open_hour is not None
        open_hour.sink.abort()
        files = sorted(open_hour.files | {rel})
        done.overlaps.setdefault(self.key, set()).update(hours)
        done.kept.update(files)
        self._store.save_done(done)
        log.error(
            "catalog hour overlap",
            extra={
                "fields": {
                    "code": "catalog_hour_overlap",
                    "stream": self.key,
                    "hours_ns": sorted(hours),
                    "files": files,
                }
            },
        )
        self._on_overlap(open_hour.hour)

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
        done.files |= {
            f for f, newest in self._newest_hour.items() if f in self.fed and newest <= hour and f not in done.kept
        }
        if self.gaps is not None:
            self._commit_gaps(done)
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
