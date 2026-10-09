"""The L2 book carried from hour to hour, and the delta flag values (spec 0007 AC-3, AC-16)."""

from typing import Any, Final

import numpy as np
import pyarrow as pa
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from trading_core.marketdata.gaps import BookScan

F_LAST: Final = 128
F_SNAPSHOT: Final = 32
# The `action` column value of a CLEAR (the pyo3 stub types `.value` as str; it is an int).
CLEAR: Final[int] = int(p3.BookAction.CLEAR.value)


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

    def apply(self, table: Any, scan: BookScan | None = None) -> None:
        """Apply a chunk of delta rows, starting at the first ``CLEAR`` when the book is invalid.

        ``scan`` marks where ``sequence`` gaps cut the book (AC-16): from a cut, rows are not
        applied, and only the ``CLEAR`` that ends the gap starts the book again.
        """
        if scan is None or not (scan.blocked or scan.cuts or scan.resumes):
            self._apply(table)
            return
        events = sorted([(i, 0) for i in scan.cuts] + [(i, 1) for i in scan.resumes])
        blocked, pos = scan.blocked, 0
        for index, resume in events:
            if not blocked and index > pos:
                self._apply(table.slice(pos, index - pos))
            pos = index
            if resume:
                blocked = False
                self.valid = False  # the book restarts at this CLEAR
            else:
                blocked = True
                self.invalidate()
        if not blocked and pos < table.num_rows:
            self._apply(table.slice(pos))

    def _apply(self, table: Any) -> None:
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
