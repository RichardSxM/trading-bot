"""covers: spec 0008 AC-4 (hour start snapshots dropped exactly, any other inner CLEAR refused) and AC-5 (quotes
from the replayed book)."""

import pytest
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDeltas, QuoteTick, TradeTick

from lab.stream import SegmentStream
from trading_core.lab.errors import UnexpectedSnapshotError
from trading_core.lab.segments import Segment

from .conftest import F_SNAPSHOT, H17, HOUR, Row, snapshot, write_hour

BID, ASK = p3.OrderSide.BUY, p3.OrderSide.SELL
UPDATE, ADD, CLEAR = p3.BookAction.UPDATE, p3.BookAction.ADD, p3.BookAction.CLEAR
H18 = H17 + HOUR
BOOK = [(BID, "100", "1"), (BID, "99", "2"), (ASK, "101", "1"), (ASK, "102", "3")]


def _events(stream: SegmentStream) -> list[OrderBookDeltas | QuoteTick | TradeTick]:
    return [event for chunk in stream.chunks() for event in chunk]


def _kinds(events: list[OrderBookDeltas | QuoteTick | TradeTick]) -> list[str]:
    names = {OrderBookDeltas: "book", QuoteTick: "quote", TradeTick: "trade"}
    return [names[type(e)] for e in events]


def test_a_quote_follows_each_batch_that_changes_the_top_of_the_book(tmp_path):
    rows = [
        *snapshot(H17, BOOK),
        Row(UPDATE, BID, "99", "5", H17 + 10),  # below the top: no quote
        Row(UPDATE, BID, "100", "4", H17 + 20),  # top size: a quote
        Row(UPDATE, ASK, "102", "1", H17 + 20),  # same message, same ts_init
    ]
    tree = write_hour(tmp_path, H17, rows, [("101", "0.5", H17 + 20)])

    events = _events(SegmentStream(tree, Segment(index=1, hours=(H17,))))

    assert _kinds(events) == ["book", "quote", "book", "book", "quote", "trade"]
    first, second = (e for e in events if isinstance(e, QuoteTick))
    assert (str(first.bid_price), str(first.ask_price), str(first.bid_size)) == ("100", "101", "1.0000")
    assert (str(second.bid_size), second.ts_init) == ("4.0000", H17 + 20)
    assert len(events[3].deltas) == 2  # one feed message, one batch


def test_the_hour_start_snapshot_of_a_later_hour_is_dropped_and_the_book_carries_on(tmp_path):
    write_hour(tmp_path, H17, [*snapshot(H17, BOOK), Row(UPDATE, BID, "100", "3", H17 + 5)], [("101", "1", H17 + 5)])
    carried = [(BID, "100", "3"), (BID, "99", "2"), (ASK, "101", "1"), (ASK, "102", "3")]
    tree = write_hour(
        tmp_path,
        H18,
        [*snapshot(H18, carried), Row(UPDATE, ASK, "101", "2", H18 + 7)],
        [("101", "1", H18 + 9)],
    )
    stream = SegmentStream(tree, Segment(index=1, hours=(H17, H18)))

    events = _events(stream)

    later = [e for e in events if e.ts_init >= H18]
    assert _kinds(later) == ["book", "quote", "trade"]
    assert later[0].ts_init == H18 + 7
    assert (str(later[1].bid_size), str(later[1].ask_size)) == ("3.0000", "2.0000")
    assert stream.last_ts_init == H18 + 7


@pytest.mark.parametrize(
    "row",
    [
        Row(CLEAR, p3.OrderSide.NO_ORDER_SIDE, "0", "0", H18 + 50, F_SNAPSHOT),
        Row(ADD, BID, "98", "1", H18 + 50, F_SNAPSHOT),
    ],
    ids=["clear", "snapshot_flag"],
)
def test_any_other_snapshot_row_inside_a_segment_is_refused(tmp_path, row):
    write_hour(tmp_path, H17, snapshot(H17, BOOK), [("101", "1", H17 + 5)])
    tree = write_hour(
        tmp_path,
        H18,
        [*snapshot(H18, BOOK), Row(UPDATE, ASK, "101", "2", H18 + 7), row],
        [("101", "1", H18 + 9)],
    )

    with pytest.raises(UnexpectedSnapshotError):
        _events(SegmentStream(tree, Segment(index=1, hours=(H17, H18))))


def test_a_second_snapshot_in_the_first_hour_is_refused(tmp_path):
    tree = write_hour(
        tmp_path,
        H17,
        [*snapshot(H17, BOOK), Row(UPDATE, BID, "100", "3", H17 + 5), *snapshot(H17 + 9, BOOK)],
        [("101", "1", H17 + 5)],
    )

    with pytest.raises(UnexpectedSnapshotError):
        _events(SegmentStream(tree, Segment(index=1, hours=(H17,))))


def test_a_later_hour_that_does_not_open_with_its_snapshot_is_refused(tmp_path):
    write_hour(tmp_path, H17, snapshot(H17, BOOK), [("101", "1", H17 + 5)])
    tree = write_hour(tmp_path, H18, [Row(UPDATE, ASK, "101", "2", H18 + 7)], [("101", "1", H18 + 9)])

    with pytest.raises(UnexpectedSnapshotError):
        _events(SegmentStream(tree, Segment(index=1, hours=(H17, H18))))
