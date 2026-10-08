"""Feather to Parquet, one hour at a time, with the book carried across hours (spec 0007).

Rows go into the open hour's Parquet writer in stream order and are applied to a carried
``nautilus_pyo3.OrderBook``. When the first row of a later hour arrives, the open hour is
finished: Parquet renamed after a row count check, the book saved, then ``done.json`` rewritten.
"""

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from recorder.compactor.store import (
    DELTAS_TABLE,
    TMP_SUFFIX,
    CatalogStore,
    DoneRecord,
    atomic_write,
    stream_key,
)
from trading_core.marketdata.errors import HourCountMismatch, HourOverlapError
from trading_core.marketdata.hours import HOUR_NS, hour_ready

log = logging.getLogger(__name__)

# The feed writes about 1.5 rows per Arrow batch. Batches are gathered into chunks of at most
# this many rows (65,536 was killed at 512 MB in Milestone 0; 8,192 held 343 MB).
CHUNK_ROWS: Final = 8_192
F_LAST: Final = 128
F_SNAPSHOT: Final = 32
# The `action` column value of a CLEAR (the pyo3 stub types `.value` as str; it is an int).
CLEAR: Final[int] = int(p3.BookAction.CLEAR.value)
COMPRESSION: Final = "zstd"


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


def read_chunks(path: Path) -> Iterator[Any]:
    """The rows of a feather stream as Arrow tables of about ``CHUNK_ROWS`` rows, in file order.

    A truncated final message (a killed recorder) ends the file at its last complete batch.
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
                log.warning(
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
        if pending:
            yield pa.Table.from_batches(pending, schema=reader.schema)


class HourSink:
    """The Parquet file of one hour being written, under ``<name>.tmp`` until its commit."""

    def __init__(self, path: Path, schema: Any, hour: int) -> None:
        self.path = path
        self.schema = schema
        self.hour = hour
        self.rows = 0
        self._tmp = path.with_name(path.name + TMP_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: Any = pq.ParquetWriter(str(self._tmp), schema, compression=COMPRESSION)

    def write(self, table: Any) -> None:
        """Append rows. The schema metadata (instrument and precisions) always comes from the hour."""
        self._writer.write_table(table.replace_schema_metadata(self.schema.metadata))
        self.rows += table.num_rows

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

    ``valid`` is false until the feed's first ``CLEAR``, and again after a restart or a gap;
    rows that arrive while it is false are written but not applied. ``hour`` is the hour whose
    end the book stands at, once that hour is finished.
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
    source_rows: int = 0
    snapshot_rows: int = 0
    last_ts: int = -1


class DeltaStream:
    """One instrument's book deltas, converted hour by hour in one pass over its closed files."""

    def __init__(self, store: CatalogStore, instrument_id: str, done: DoneRecord) -> None:
        self._store = store
        self.instrument_id = instrument_id
        self.key = stream_key(DELTAS_TABLE, instrument_id)
        self.carry = BookCarry(instrument_id)
        self.open: _OpenHour | None = None
        self._run: str | None = None
        self._newest_hour: dict[str, int] = {}  # fed file -> the newest hour it held
        self.fed: set[str] = set()
        carried = done.carry.get(instrument_id)
        book = store.book_file(instrument_id, carried) if carried is not None else None
        if carried is not None and book is not None and book.exists():
            self.carry.restore(pq.read_table(str(book)), carried)

    def feed(self, file: FeatherFile, done: DoneRecord) -> None:
        """Read one closed file from start to end, finishing every hour that the stream passes."""
        rel = str(file.path.relative_to(self._store.root))
        if rel in done.files or rel in self.fed:
            return
        if self._run is not None and file.run != self._run:
            # A restart: the old book no longer matches the feed until its next CLEAR.
            self.carry.invalidate()
        self._run = file.run
        last_done = done.last_hour(self.key)
        for chunk in read_chunks(file.path):
            ts = chunk.column("ts_init").to_numpy()
            hours = ts - ts % HOUR_NS
            cuts = (np.flatnonzero(np.diff(hours)) + 1).tolist()
            for start, end in zip([0, *cuts], [*cuts, len(ts)], strict=True):
                hour = int(hours[start])
                self._newest_hour[rel] = max(self._newest_hour.get(rel, hour), hour)
                if last_done is not None and hour <= last_done:
                    continue  # finished before a restart; the carried book already holds it
                self._write(hour, chunk.slice(start, end - start), ts[start:end], done)
        self.fed.add(rel)

    def finish_if_passed(self, *, now_ns: int, grace_ns: int, more_source: bool, done: DoneRecord) -> None:
        """Finish the open hour when no file that could hold more of its rows is left to read."""
        if self.open is None or more_source:
            return
        if hour_ready(self.open.hour, now_ns=now_ns, grace_ns=grace_ns, positions={}, run_ended=True):
            self._finish(done)

    def abort(self) -> None:
        """Drop the open hour after a failed pass; the next pass starts again from ``done.json``."""
        if self.open is not None:
            self.open.sink.abort()
            self.open = None

    def _write(self, hour: int, rows: Any, ts: Any, done: DoneRecord) -> None:
        if self.open is None:
            self._start(hour, rows.schema)
        elif hour > self.open.hour:
            self._finish(done)
            self._start(hour, rows.schema)
        assert self.open is not None
        open_hour = self.open
        if hour < open_hour.hour or int(ts[0]) < open_hour.last_ts or bool((np.diff(ts) < 0).any()):
            raise HourOverlapError(open_hour.hour, open_hour.last_ts, int(ts.min()))
        open_hour.sink.write(rows)
        open_hour.source_rows += rows.num_rows
        open_hour.last_ts = int(ts[-1])
        self.carry.apply(rows)

    def _start(self, hour: int, schema: Any) -> None:
        sink = HourSink(self._store.data_file(DELTAS_TABLE, self.instrument_id, hour), schema, hour)
        self.open = _OpenHour(hour=hour, sink=sink)
        if self.carry.valid and self.carry.hour == hour - HOUR_NS:
            snapshot = self.carry.snapshot_rows(hour, schema)
            sink.write(snapshot)
            self.open.snapshot_rows = snapshot.num_rows
        elif self.carry.hour is not None:
            # Hours without rows lie between: the book is stale until the feed's next CLEAR.
            self.carry.invalidate()

    def _finish(self, done: DoneRecord) -> None:
        open_hour, self.open = self.open, None
        assert open_hour is not None
        hour = open_hour.hour
        expected = open_hour.source_rows + open_hour.snapshot_rows
        size = open_hour.sink.commit(expected)
        if self.carry.valid:
            book = self.carry.snapshot_rows(hour + HOUR_NS, open_hour.sink.schema)
            atomic_write(
                self._store.book_file(self.instrument_id, hour),
                lambda tmp: pq.write_table(book, str(tmp), compression=COMPRESSION),
            )
            self.carry.hour = hour
        done.hours.setdefault(self.key, {})[hour] = {
            "rows": expected,
            "bytes": size,
            "snapshot_rows": open_hour.snapshot_rows,
        }
        done.carry[self.instrument_id] = hour if self.carry.valid else None
        done.files |= {f for f, newest in self._newest_hour.items() if f in self.fed and newest <= hour}
        self._store.save_done(done)
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
                }
            },
        )
