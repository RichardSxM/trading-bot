"""The hour stream: one segment's recorded hours as the engine's data, one hour at a time (spec 0008 AC-4, AC-5, AC-16).

For each hour it reads the book deltas in row group batches and the trades, groups the deltas
into feed messages (a batch ends at ``F_LAST`` or when ``ts_init`` changes), and merges both
streams by ``ts_init`` (book before trades at the same nanosecond). It keeps its own L2 book and
emits a ``QuoteTick`` right after every batch that changes the top of the book, so quotes reach
the data engine, the cache, and bar aggregation exactly like live quotes.

Every hour after the segment's first opens with the compactor's synthetic snapshot of the book
carried so far. That run of rows (from its ``CLEAR`` to the next ``F_LAST``, at the hour's first
``ts_init``) is dropped: the book is already right there, and replaying it would move resting
orders to the front of their queues. Any other ``CLEAR`` or snapshot row after the segment's
first batch raises ``UnexpectedSnapshotError``.

With ``final_ts_init`` set, the stream ends with one more batch at that time that leaves the book
as it is, so the runner's segment end cancel and close reach the venue and fill against the
last book (AC-8).
"""

import heapq
import logging
import os
import resource
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
from nautilus_trader.core.data import Data
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, QuoteTick, TradeTick
from nautilus_trader.model.enums import BookAction, BookType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.catalog import DELTAS_TABLE, TRADES_TABLE, SourceTree
from trading_core.lab.errors import UnexpectedSnapshotError
from trading_core.lab.segments import Segment

log = logging.getLogger(__name__)

F_SNAPSHOT: Final = 32
F_LAST: Final = 128
ROW_BATCH: Final = 65_536
CHUNK: Final = 20_000
_PAGE: Final = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

type Top = tuple[int, int, int, int]


def rss_bytes() -> int:
    """The process's current resident memory (Linux ``/proc``), else its peak."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            return int(statm.read().split()[1]) * _PAGE
    except OSError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def read_deltas(path: Path) -> Iterator[OrderBookDelta]:
    """The hour's deltas in file order, read a row group batch at a time."""
    parquet = pq.ParquetFile(path)
    schema = parquet.schema_arrow
    for batch in parquet.iter_batches(batch_size=ROW_BATCH):
        table = pa.Table.from_batches([batch]).replace_schema_metadata(schema.metadata)
        yield from OrderBookDelta.from_pyo3_list(ArrowSerializer.deserialize(OrderBookDelta, table))


def read_trades(path: Path) -> list[TradeTick]:
    """The hour's trades (a small file, read whole)."""
    trades: list[TradeTick] = TradeTick.from_pyo3_list(ArrowSerializer.deserialize(TradeTick, pq.read_table(path)))
    return trades


def _is_snapshot_row(delta: OrderBookDelta) -> bool:
    return delta.action == BookAction.CLEAR or bool(delta.flags & F_SNAPSHOT)


class SegmentStream:
    """The data of one segment, fed to ``BacktestEngine.add_data_iterator`` through ``chunks()``.

    ``on_hour`` is called after each hour is fully handed over, with the hour, its event count,
    and the wall seconds it took (the engine pulls chunks as it goes, so this is replay time).
    ``last_ts_init`` is the newest delta's ``ts_init`` seen so far.
    """

    def __init__(
        self,
        tree: SourceTree,
        segment: Segment,
        *,
        on_hour: Callable[[int, int, float], None] | None = None,
        chunk: int = CHUNK,
        final_ts_init: int | None = None,
    ) -> None:
        self._tree = tree
        self._segment = segment
        self._final_ts_init = final_ts_init
        self._instrument_id = InstrumentId.from_str(tree.instrument_id)
        self._book = OrderBook(self._instrument_id, BookType.L2_MBP)
        self._top: Top | None = None
        self._on_hour = on_hour
        self._chunk = chunk
        self._first_batch_done = False
        self.last_ts_init: int | None = None
        self.events = 0

    def chunks(self) -> Iterator[list[Data]]:
        """Lists of at most ``chunk`` events in ``ts_init`` order, one hour after another."""
        for position, hour in enumerate(self._segment.hours):
            started = time.monotonic()
            count = 0
            out: list[Data] = []
            for event in self._hour_events(hour, inner=position > 0):
                out.append(event)
                count += 1
                if len(out) >= self._chunk:
                    yield out
                    out = []
            if out:
                yield out
            self.events += count
            if self._on_hour is not None:
                self._on_hour(hour, count, time.monotonic() - started)
        final = self._final_batch()
        if final is not None:
            yield [final]

    def last_mid(self) -> Decimal | None:
        """The mid of the last book, or None while either side is empty."""
        bid, ask = self._book.best_bid_price(), self._book.best_ask_price()
        if bid is None or ask is None:
            return None
        mid: Decimal = (bid.as_decimal() + ask.as_decimal()) / 2
        return mid

    def _final_batch(self) -> OrderBookDeltas | None:
        """The segment end batch (AC-8): one ``UPDATE`` of the best level to the size it has, at
        ``final_ts_init``, so the engine reaches that time with the last book unchanged. The engine
        refuses an empty batch, so this is the smallest batch that changes nothing."""
        if self._final_ts_init is None:
            return None
        if self._final_ts_init <= (self.last_ts_init or 0):
            raise ValueError(f"final batch at {self._final_ts_init} is not after the last delta {self.last_ts_init}")
        bid, ask = self._book.best_bid_price(), self._book.best_ask_price()
        if bid is not None:
            order = BookOrder(OrderSide.BUY, bid, self._book.best_bid_size(), 0)
        elif ask is not None:
            order = BookOrder(OrderSide.SELL, ask, self._book.best_ask_size(), 0)
        else:
            return None
        ts = self._final_ts_init
        delta = OrderBookDelta(self._instrument_id, BookAction.UPDATE, order, F_LAST, 0, ts, ts)
        batch = OrderBookDeltas(self._instrument_id, [delta])
        self._book.apply_deltas(batch)
        return batch

    def _hour_events(self, hour: int, *, inner: bool) -> Iterator[Data]:
        deltas = self._book_events(read_deltas(self._tree.hour_file(DELTAS_TABLE, hour)), inner=inner)
        trades = read_trades(self._tree.hour_file(TRADES_TABLE, hour))
        keyed_book = ((e.ts_init, 0, n, e) for n, e in enumerate(deltas))
        keyed_trades = ((t.ts_init, 1, n, t) for n, t in enumerate(trades))
        for _, _, _, event in heapq.merge(keyed_book, keyed_trades):
            yield event

    def _book_events(self, deltas: Iterator[OrderBookDelta], *, inner: bool) -> Iterator[Data]:
        for batch in self._batches(deltas, drop_hour_snapshot=inner):
            self._book.apply_deltas(batch)
            self.last_ts_init = batch.ts_init
            yield batch
            quote = self._quote(batch.ts_init)
            if quote is not None:
                yield quote

    def _batches(self, deltas: Iterator[OrderBookDelta], *, drop_hour_snapshot: bool) -> Iterator[OrderBookDeltas]:
        pending: list[OrderBookDelta] = []
        dropping = drop_hour_snapshot
        first_ts: int | None = None
        for delta in deltas:
            if dropping:
                if first_ts is None:
                    first_ts = delta.ts_init
                    if delta.action != BookAction.CLEAR:
                        raise UnexpectedSnapshotError(f"hour at {first_ts} does not open with its snapshot")
                if delta.ts_init != first_ts:
                    raise UnexpectedSnapshotError(f"hour start snapshot at {first_ts} has no F_LAST row")
                if delta.flags & F_LAST:
                    dropping = False
                continue
            if pending and delta.ts_init != pending[-1].ts_init:
                yield self._emit(pending)
                pending = []
            if self._first_batch_done and _is_snapshot_row(delta):
                raise UnexpectedSnapshotError(
                    f"{'CLEAR' if delta.action == BookAction.CLEAR else 'snapshot'} row at ts_init "
                    f"{delta.ts_init} inside segment {self._segment.index}"
                )
            pending.append(delta)
            if delta.flags & F_LAST:
                yield self._emit(pending)
                pending = []
        if dropping:
            raise UnexpectedSnapshotError(f"hour start snapshot at {first_ts} never ends")
        if pending:
            yield self._emit(pending)

    def _emit(self, pending: list[OrderBookDelta]) -> OrderBookDeltas:
        self._first_batch_done = True
        return OrderBookDeltas(self._instrument_id, pending)

    def _quote(self, ts_init: int) -> QuoteTick | None:
        bid, ask = self._book.best_bid_price(), self._book.best_ask_price()
        if bid is None or ask is None:
            return None
        bid_size, ask_size = self._book.best_bid_size(), self._book.best_ask_size()
        top: Top = (bid.raw, ask.raw, bid_size.raw, ask_size.raw)
        if top == self._top:
            return None
        self._top = top
        return QuoteTick(self._instrument_id, bid, ask, bid_size, ask_size, ts_init, ts_init)
