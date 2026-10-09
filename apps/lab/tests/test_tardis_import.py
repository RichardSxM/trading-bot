"""covers: spec 0008 AC-1 (Tardis day import) and AC-2 (catalog cap, prune).

The CSV files are tiny synthetic days in Tardis's real column layout, so every row is known: a
first snapshot block near the end of hour 00, a second block 80 ms after an update in hour 01
(diffed, no gap), a third block equal to the book after a 12 s pause (a ``reconnect`` gap and no
rows), and a 17 s pause between updates (a ``silence`` gap).
"""

import gzip
import logging
from datetime import date
from decimal import Decimal

import pyarrow.parquet as pq
import pytest
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.catalog import DELTAS_TABLE, INSTRUMENTS_TABLE, TRADES_TABLE, SourceTree
from lab.main import main
from lab.settings import LabSettings
from lab.stream import SegmentStream
from lab.tardis_import import (
    BOOK_CSV,
    EXIT_CATALOG_FULL,
    EXIT_FAILED,
    EXIT_OK,
    TICKER_CSV,
    TRADES_CSV,
    import_tardis,
    prune,
    raw_file,
)
from trading_core.catalog.book import BookCarry
from trading_core.catalog.days import read_gaps
from trading_core.lab.segments import Segment
from trading_core.marketdata.gaps import Gap

from .conftest import F_LAST, F_SNAPSHOT, INSTRUMENT, lab_settings

DAY = date(2026, 9, 1)
D0_US = 1_788_220_800_000_000  # 2026-09-01 00:00 UTC in microseconds
H00 = D0_US * 1000
H01 = H00 + 3_600_000_000_000
CLEAR = int(p3.BookAction.CLEAR.value)
VENUE = ("cryptofacilities", "PF_XBTUSD")
TICKER_REST = (1, 100, 100, 100)  # open interest, last, index, and mark price


def us(hh, mm, ss, ms=0):
    return D0_US + ((hh * 60 + mm) * 60 + ss) * 1_000_000 + ms * 1000


def ns(hh, mm, ss, ms=0):
    return us(hh, mm, ss, ms) * 1000


FIRST = [("bid", 100, "1"), ("bid", 99, "2"), ("ask", 101, "1"), ("ask", 102, "2")]
SECOND = [("bid", 100, "4"), ("bid", 98, "5"), ("ask", 101, "3"), ("ask", 102, "2")]
THIRD = [("bid", 100, "4"), ("bid", 98, "5"), ("ask", 101, "2"), ("ask", 102, "2")]

BOOK = [
    (us(0, 59, 50), True, FIRST),
    (us(0, 59, 55), False, [("ask", 101, "3")]),
    (us(1, 0, 0, 500), False, [("bid", 100, "4")]),
    (us(1, 0, 0, 580), True, SECOND),  # 80 ms after the update: diffed, no gap
    (us(1, 0, 0, 600), False, [("ask", 101, "2")]),
    (us(1, 0, 12, 600), True, THIRD),  # 12 s pause, equal to the book: reconnect gap, no rows
    (us(1, 0, 13), False, [("ask", 102, "1")]),
    (us(1, 0, 30), False, [("bid", 98, "0")]),  # 17 s pause: silence gap
    (us(1, 0, 31), False, [("bid", 97, "0.5")]),
]


def _csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as fh:
        fh.write(header + "\n")
        for row in rows:
            fh.write(",".join(str(v) for v in row) + "\n")


def write_day(raw_dir, *, price_text=None):
    """The three CSV files of the synthetic day in ``raw_dir``."""
    book = []
    for local, snapshot, levels in BOOK:
        for side, price, amount in levels:
            text = price_text if price_text is not None and price == 99 else price
            book.append((*VENUE, local - 10_000, local, str(snapshot).lower(), side, text, amount))
    _csv(
        raw_file(raw_dir, BOOK_CSV, DAY),
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount",
        book,
    )
    _csv(
        raw_file(raw_dir, TRADES_CSV, DAY),
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount",
        [
            (*VENUE, us(0, 59, 52) - 5000, us(0, 59, 52), "t1", "buy", 101, "0.1"),
            (*VENUE, us(1, 0, 5) - 5000, us(1, 0, 5), "t2", "sell", 100, "0.2"),
        ],
    )
    funding = 1_788_224_400_000_000  # 01:00
    _csv(
        raw_file(raw_dir, TICKER_CSV, DAY),
        "exchange,symbol,timestamp,local_timestamp,funding_timestamp,funding_rate,predicted_funding_rate,"
        "open_interest,last_price,index_price,mark_price",
        [
            (*VENUE, us(0, 59, 50) - 1, us(0, 59, 50), funding, "0.0001", "0.0001", *TICKER_REST),
            (*VENUE, us(0, 59, 51) - 1, us(0, 59, 51), funding, "0.0001", "0.0001", *TICKER_REST),
            (*VENUE, us(1, 0, 2) - 1, us(1, 0, 2), funding + 3_600_000_000, "0.0002", "0", *TICKER_REST),
        ],
    )


class Downloads:
    """Stands in for ``tardis-dev``: writes the synthetic CSV files and counts the calls."""

    def __init__(self, **kwargs):
        self.calls = 0
        self.kwargs = kwargs

    def __call__(self, day, raw_dir):
        self.calls += 1
        write_day(raw_dir, **self.kwargs)


def settings(tmp_path, **overrides) -> LabSettings:
    base = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    return base.model_copy(update=overrides)


def tree(tmp_path):
    return SourceTree(tmp_path / "catalog" / "tardis", INSTRUMENT)


def deltas(path):
    return ArrowSerializer.deserialize(OrderBookDelta, pq.read_table(path))


def every_file(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_day_converts_into_hours_that_start_with_snapshots(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    downloads = Downloads()

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    t = tree(tmp_path)
    assert t.manifest_file(DAY).is_file()
    assert not (t.root / ".raw" / DAY.isoformat()).exists()
    hour0, hour1 = deltas(t.hour_file(DELTAS_TABLE, H00)), deltas(t.hour_file(DELTAS_TABLE, H01))
    # Hour 00: the day's first block as it came (CLEAR, four levels), then one update.
    assert hour0[0].action.value == CLEAR
    assert len(hour0) == 6
    # Hour 01: the carried book as a snapshot at 01:00, then only level changes.
    snapshot = hour1[:5]
    assert snapshot[0].action.value == CLEAR
    assert all(d.flags & F_SNAPSHOT and d.ts_init == H01 for d in snapshot)
    assert snapshot[-1].flags & F_LAST
    assert not any(d.action.value == CLEAR or d.flags & F_SNAPSHOT for d in hour1[5:])
    # The second block is the delete of bid 99 and the add of bid 98, at the block's own times.
    diff = [d for d in hour1 if d.ts_init == ns(1, 0, 0, 580)]
    assert [(d.action.name, d.order.side.name, str(d.order.price)) for d in diff] == [
        ("DELETE", "BUY", "99"),
        ("ADD", "BUY", "98"),
    ]
    assert [d.flags for d in diff] == [0, F_LAST]
    assert {d.ts_event for d in diff} == {ns(1, 0, 0, 570)}
    # The third block matched the book: nothing at its time.
    assert not [d for d in hour1 if d.ts_init == ns(1, 0, 12, 600)]
    assert len(hour1) == 5 + 1 + 2 + 1 + 3

    carry = BookCarry(INSTRUMENT)
    carry.apply(pq.read_table(t.hour_file(DELTAS_TABLE, H01)))
    tops = [lv.first() for lv in [*carry.book.bids(), *carry.book.asks()]]
    levels = [(str(o.price), str(o.size)) for o in tops if o is not None]
    assert levels == [("100", "4.0000"), ("97", "0.5000"), ("101", "2.0000"), ("102", "1.0000")]

    assert read_gaps(t.layout, DAY.isoformat()) == [
        Gap(ns(1, 0, 0, 600), INSTRUMENT, "reconnect", ns(1, 0, 12, 600)),
        Gap(ns(1, 0, 13), INSTRUMENT, "silence", ns(1, 0, 30)),
    ]
    assert [pq.read_table(t.hour_file(TRADES_TABLE, hour)).num_rows for hour in (H00, H01)] == [1, 1]
    rates = [(r.ts_ns, r.rate) for r in t.funding_rates([DAY])]
    assert rates == [(ns(0, 59, 50), Decimal("0.0001")), (ns(1, 0, 2), Decimal("0.0002"))]  # the repeat is left out
    assert pq.read_table(t.hour_file(INSTRUMENTS_TABLE, H01)).num_rows == 1

    done = next(r for r in caplog.records if r.getMessage() == "lab_import_day_done")
    assert (done.fields["snapshot_diffs"], done.fields["gaps"]) == (2, 2)


def test_the_converted_hours_replay_without_an_inner_snapshot(tmp_path):
    import_tardis(settings(tmp_path), [DAY], download=Downloads())

    # Both hours as one segment: the stream drops hour 01's start snapshot and finds no other CLEAR.
    events = [e for chunk in SegmentStream(tree(tmp_path), Segment(1, (H00, H01))).chunks() for e in chunk]

    assert events


def test_a_rerun_skips_the_day(tmp_path, caplog):
    downloads = Downloads()
    import_tardis(settings(tmp_path), [DAY], download=downloads)
    before = every_file(tmp_path / "catalog")
    caplog.set_level(logging.INFO)

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    assert downloads.calls == 1
    assert every_file(tmp_path / "catalog") == before
    assert "lab_import_day_skipped" in [r.getMessage() for r in caplog.records]


def test_a_day_killed_before_its_manifest_converts_again_to_the_same_bytes(tmp_path):
    import_tardis(settings(tmp_path), [DAY], download=Downloads())
    before = every_file(tmp_path / "catalog")
    t = tree(tmp_path)
    t.manifest_file(DAY).unlink()  # as if killed just before the commit
    t.hour_file(TRADES_TABLE, H01).write_bytes(b"half written")

    assert import_tardis(settings(tmp_path), [DAY], download=Downloads()) == EXIT_OK

    assert every_file(tmp_path / "catalog") == before


def test_a_price_off_the_tick_refuses_the_day(tmp_path, caplog):
    assert import_tardis(settings(tmp_path), [DAY], download=Downloads(price_text="99.5")) == EXIT_FAILED

    assert not tree(tmp_path).manifest_file(DAY).exists()
    assert "lab_tick_mismatch" in [r.getMessage() for r in caplog.records]


def test_the_cap_stops_the_import_before_a_download(tmp_path, caplog):
    downloads = Downloads()

    code = import_tardis(settings(tmp_path, lab_catalog_cap_gb=Decimal(1)), [DAY], download=downloads)

    assert code == EXIT_CATALOG_FULL
    assert downloads.calls == 0
    assert "lab_catalog_full" in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("days", [["2026-09-02", "2026-09-02"], ["2026-09-01", "2026-09-15"]])
def test_a_day_that_is_not_a_first_exits_64(days, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["import-tardis", *days])

    assert exit_.value.code == 64
    assert "first of each month only" in capsys.readouterr().err


def test_prune_deletes_one_tree_and_counts_its_bytes(tmp_path):
    s = settings(tmp_path)
    for source, size in (("tardis", 300), ("own", 50)):
        path = s.lab_catalog_dir / source / "data" / "x.parquet"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * size)

    assert prune(s, "tardis") == 300
    assert not (s.lab_catalog_dir / "tardis").exists()
    assert (s.lab_catalog_dir / "own").exists()
    assert prune(s, None) == 50
    assert not (s.lab_catalog_dir / "own").exists()
