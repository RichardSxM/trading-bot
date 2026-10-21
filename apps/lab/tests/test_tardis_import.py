"""covers: spec 0008 AC-1 (Tardis day import) and AC-2 (catalog cap, prune), spec 0016 AC-1 to AC-4 (off tick
trades: dropped, the 5% cap, the raw CSV never changed).

The CSV files are tiny synthetic days in Tardis's real column layout, so every row is known: a
first snapshot block near the end of hour 00, a second block 80 ms after an update in hour 01
(diffed, no gap), a third block equal to the book after a 12 s pause (a ``reconnect`` gap and no
rows), and a 17 s pause between updates (a ``silence`` gap).
"""

import gzip
import json
import logging
from datetime import date
from decimal import Decimal

import pyarrow.parquet as pq
import pytest
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab import tardis_import
from lab.catalog import DELTAS_TABLE, INSTRUMENTS_TABLE, TRADES_TABLE, SourceTree
from lab.main import main
from lab.settings import LabSettings
from lab.stream import SegmentStream
from lab.tardis_import import (
    BOOK_CSV,
    EXIT_CATALOG_FULL,
    EXIT_FAILED,
    EXIT_OK,
    KEPT_SUFFIX,
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


def write_day(
    raw_dir, *, price_text=None, trade_price_text=None, trade_size_text=None, off_trades=1, filler=0, no_trades=False
):
    """The three CSV files of the synthetic day in ``raw_dir``.

    ``trade_price_text`` adds ``off_trades`` trades at that price, and ``filler`` adds trades on the tick,
    all in the second after 01:00:05, so the shares off the tick come out exact. ``no_trades`` writes
    only the header.
    """
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
        []
        if no_trades
        else [
            (*VENUE, us(0, 59, 52) - 5000, us(0, 59, 52), "t1", "buy", 101, "0.1"),
            (*VENUE, us(1, 0, 5) - 5000, us(1, 0, 5), "t2", "sell", 100, "0.2"),
            *(
                (*VENUE, us(1, 0, 5, 1 + i) - 5000, us(1, 0, 5, 1 + i), f"f{i}", "buy", 100, "0.1")
                for i in range(filler)
            ),
            *(
                [
                    (*VENUE, us(1, 0, 6, i) - 5000, us(1, 0, 6, i), f"t3{i}", "buy", trade_price_text, "0.0004")
                    for i in range(off_trades)
                ]
                if trade_price_text is not None
                else []
            ),
            *(
                [(*VENUE, us(1, 0, 7) - 5000, us(1, 0, 7), "t4", "buy", 100, trade_size_text)]
                if trade_size_text is not None
                else []
            ),
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
    # Every row, repeats too, so its age matches our own funding (spec 0009 AC-15).
    assert rates == [
        (ns(0, 59, 50), Decimal("0.0001")),
        (ns(0, 59, 51), Decimal("0.0001")),
        (ns(1, 0, 2), Decimal("0.0002")),
    ]
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


def test_a_trade_price_off_the_tick_drops_only_that_trade(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    downloads = Downloads(trade_price_text="27567.42278253538", filler=18)  # 1 of 21 rows

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    t = tree(tmp_path)
    assert t.manifest_file(DAY).is_file()
    assert [pq.read_table(t.hour_file(TRADES_TABLE, hour)).num_rows for hour in (H00, H01)] == [1, 19]
    warned = next(r for r in caplog.records if r.getMessage() == "lab_off_tick_trades_dropped")
    assert (warned.fields["rows"], warned.fields["prices"]) == (1, ["27567.42278253538"])
    done = next(r for r in caplog.records if r.getMessage() == "lab_import_day_done")
    assert done.fields["off_tick_trades"] == 1


def test_exactly_5_percent_off_the_tick_imports(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    downloads = Downloads(trade_price_text="27567.42278253538", off_trades=5, filler=93)  # 5 of 100

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    t = tree(tmp_path)
    assert sum(pq.read_table(t.hour_file(TRADES_TABLE, hour)).num_rows for hour in (H00, H01)) == 95
    done = next(r for r in caplog.records if r.getMessage() == "lab_import_day_done")
    assert done.fields["off_tick_trades"] == 5


def test_more_than_5_percent_off_the_tick_refuses_the_day_and_drops_nothing(tmp_path, caplog):
    downloads = Downloads(trade_price_text="27567.42278253538", off_trades=6, filler=92)  # 6 of 100
    raw = tmp_path / "catalog" / "tardis" / ".raw" / DAY.isoformat()

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_FAILED

    assert not tree(tmp_path).manifest_file(DAY).exists()
    refused = next(r for r in caplog.records if r.getMessage() == "lab_off_tick_trades_excess")
    assert refused.levelname == "ERROR"
    assert (refused.fields["day"], refused.fields["rows"], refused.fields["total"]) == ("2026-09-01", 6, 100)
    assert refused.fields["prices"] == ["27567.42278253538"]
    assert "lab_off_tick_trades_dropped" not in [r.getMessage() for r in caplog.records]
    # Nothing dropped: no filtered copy, and the raw CSV still holds every row.
    assert not list(raw.glob(f"*{KEPT_SUFFIX}*"))
    with gzip.open(raw_file(raw, TRADES_CSV, DAY), "rt") as fh:
        assert len(fh.read().splitlines()) == 101


def test_a_day_with_no_trade_rows_imports(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    assert import_tardis(settings(tmp_path), [DAY], download=Downloads(no_trades=True)) == EXIT_OK

    done = next(r for r in caplog.records if r.getMessage() == "lab_import_day_done")
    assert done.fields["off_tick_trades"] == 0


def test_a_rerun_of_a_killed_day_counts_the_same_off_tick_trades(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    downloads = Downloads(trade_price_text="27567.42278253538", off_trades=2, filler=96)  # 2 of 100
    raw = tmp_path / "catalog" / "tardis" / ".raw" / DAY.isoformat()

    def killed(*args):
        raise RuntimeError("killed")

    monkeypatch.setattr(tardis_import, "convert_trades", killed)
    with pytest.raises(RuntimeError, match="killed"):
        import_tardis(settings(tmp_path), [DAY], download=downloads)
    # The kept rows went to the copy; the raw CSV still holds all 100 trade rows.
    assert len(list(raw.glob(f"*{KEPT_SUFFIX}"))) == 1
    with gzip.open(raw_file(raw, TRADES_CSV, DAY), "rt") as fh:
        assert len(fh.read().splitlines()) == 101
    monkeypatch.undo()
    caplog.clear()

    assert import_tardis(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    assert downloads.calls == 1  # the raw files were still there
    warned = next(r for r in caplog.records if r.getMessage() == "lab_off_tick_trades_dropped")
    assert warned.fields["rows"] == 2
    assert not raw.exists()  # both files go with the raw folder once the manifest exists


def test_a_clean_day_drops_no_trade(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    import_tardis(settings(tmp_path), [DAY], download=Downloads())

    assert "lab_off_tick_trades_dropped" not in [r.getMessage() for r in caplog.records]
    done = next(r for r in caplog.records if r.getMessage() == "lab_import_day_done")
    assert done.fields["off_tick_trades"] == 0


def test_a_trade_size_off_the_lot_still_refuses_the_day(tmp_path, caplog):
    assert import_tardis(settings(tmp_path), [DAY], download=Downloads(trade_size_text="0.00005")) == EXIT_FAILED

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


def test_prune_reports_a_tree_it_could_not_delete(tmp_path, monkeypatch):
    s = settings(tmp_path)
    (s.lab_catalog_dir / "tardis").mkdir(parents=True)

    def refuse(path, ignore_errors=False, **kwargs):
        if not ignore_errors:  # like shutil.rmtree, which then skips what it could not delete
            raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("lab.tardis_import.shutil.rmtree", refuse)
    with pytest.raises(PermissionError):
        prune(s, "tardis")


def test_the_ticker_gives_mark_index_and_open_interest_rows(tmp_path):
    """covers: spec 0009 AC-15."""
    from nautilus_trader.model.data import IndexPriceUpdate, MarkPriceUpdate

    from lab.catalog import INDEX_TABLE, MARK_TABLE, OPEN_INTEREST_TABLE
    from trading_core.catalog.context import read_price_updates
    from trading_core.node.context_types import OpenInterestUpdate

    import_tardis(settings(tmp_path), [DAY], download=Downloads())

    t = tree(tmp_path)
    marks = [
        u for h in (H00, H01) for u in read_price_updates(MarkPriceUpdate, pq.read_table(t.hour_file(MARK_TABLE, h)))
    ]
    assert [(u.ts_init, u.ts_event, str(u.value)) for u in marks] == [
        (ns(0, 59, 50), ns(0, 59, 50) - 1_000, "100"),
        (ns(0, 59, 51), ns(0, 59, 51) - 1_000, "100"),
        (ns(1, 0, 2), ns(1, 0, 2) - 1_000, "100"),
    ]
    index = read_price_updates(IndexPriceUpdate, pq.read_table(t.hour_file(INDEX_TABLE, H01)))
    assert [str(u.value) for u in index] == ["100"]
    oi = ArrowSerializer.deserialize(OpenInterestUpdate, pq.read_table(t.hour_file(OPEN_INTEREST_TABLE, H00)))
    assert [str(u.value) for u in oi] == ["1.0000", "1.0000"]
    manifest = json.loads(t.manifest_file(DAY).read_text())
    assert {s["type"] for s in manifest["streams"]} >= {MARK_TABLE, INDEX_TABLE, OPEN_INTEREST_TABLE}


def test_context_backfill_rewrites_only_the_context_of_a_done_day(tmp_path, caplog):
    """covers: spec 0009 AC-15 (the --context backfill, its commit point, reruns)."""
    from lab.catalog import MARK_TABLE
    from lab.tardis_import import context_manifest_file, import_context

    import_tardis(settings(tmp_path), [DAY], download=Downloads())
    t = tree(tmp_path)
    for hour in (H00, H01):
        t.hour_file(MARK_TABLE, hour).unlink()  # as a day imported before spec 0009
    gap_before = t.layout.gap_file(DAY.isoformat()).read_bytes()
    manifest_before = t.manifest_file(DAY).read_bytes()
    downloads = Downloads()

    assert import_context(settings(tmp_path), [DAY], download=downloads) == EXIT_OK

    assert t.hour_file(MARK_TABLE, H00).is_file()
    assert t.layout.gap_file(DAY.isoformat()).read_bytes() == gap_before
    assert t.manifest_file(DAY).read_bytes() == manifest_before
    context = json.loads(context_manifest_file(t, DAY).read_text())
    assert context["day"] == DAY.isoformat()
    assert context["tables"][MARK_TABLE]["rows"] == 3
    assert context["tables"]["funding_rate_update"]["rows"] == 3
    assert not (t.root / ".raw").exists() or not any((t.root / ".raw").iterdir())
    after = every_file(tmp_path / "catalog")

    caplog.set_level(logging.INFO)
    assert import_context(settings(tmp_path), [DAY], download=downloads) == EXIT_OK
    assert downloads.calls == 1
    assert "lab_import_day_skipped" in [r.getMessage() for r in caplog.records]

    context_manifest_file(t, DAY).unlink()  # killed before the commit point
    assert import_context(settings(tmp_path), [DAY], download=Downloads()) == EXIT_OK
    assert every_file(tmp_path / "catalog") == after


def test_context_backfill_skips_a_day_not_imported(tmp_path, caplog):
    """covers: spec 0009 AC-15."""
    from lab.tardis_import import import_context

    downloads = Downloads()

    assert import_context(settings(tmp_path), [DAY], download=downloads) == EXIT_OK
    assert downloads.calls == 0
    assert "lab_context_day_not_imported" in [r.getMessage() for r in caplog.records]


def test_a_failed_context_download_leaves_the_days_funding_in_place(tmp_path):
    """covers: spec 0009 AC-15 (a backfill never leaves a done day without its funding)."""
    from lab.tardis_import import context_manifest_file, import_context_day

    import_tardis(settings(tmp_path), [DAY], download=Downloads())
    t = tree(tmp_path)
    funding = t.hour_file("funding_rate_update", H00)
    before = funding.read_bytes()

    def fail(day, raw_dir):
        raise OSError("download failed")

    with pytest.raises(OSError, match="download failed"):
        import_context_day(t, DAY, download=fail)

    assert funding.read_bytes() == before
    assert not context_manifest_file(t, DAY).exists()
