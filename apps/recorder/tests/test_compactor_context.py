"""covers: spec 0009 AC-2 (context streams convert like trades)."""

import time
from decimal import Decimal

import pyarrow.parquet as pq
from feed_fixture import (
    H0,
    HOUR_NS,
    INSTRUMENT,
    SECOND_NS,
    liquidation,
    make_feed,
    make_funding,
    make_open_interest,
    make_prices,
    write_context,
    write_run,
)
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from recorder.compactor.main import Compactor
from recorder.compactor.store import FUNDING_TABLE, CatalogStore, stream_key
from trading_core.marketdata.gaps import Gap


def _later():
    return time.time_ns() + 10 * HOUR_NS


def _compactor(store):
    return Compactor(store, grace_ns=0, floor_bytes=0)


def _runs(tmp_path, funding, *, end_ns=H0 + 2 * HOUR_NS - SECOND_NS):
    """A dead run with the book over two hours plus ``funding``, and a current run hours later."""
    store = CatalogStore(tmp_path)
    rotate = [H0 + HOUR_NS, H0 + 2 * HOUR_NS]
    write_run(store.live, "a-dead", make_feed(H0 + 7 * SECOND_NS, end_ns).messages, rotate_at=rotate)
    write_context(store.live, "a-dead", FUNDING_TABLE, funding, rotate_at=rotate)
    later = make_feed(H0 + 5 * HOUR_NS, H0 + 5 * HOUR_NS + 60 * SECOND_NS, seed=3)
    write_run(store.live, "b-current", later.messages, rotate_at=[])
    return store


def _read(store, hour):
    path = store.data_file(FUNDING_TABLE, INSTRUMENT, hour)
    return ArrowSerializer.deserialize(FundingRateUpdate, pq.read_table(path))


def test_funding_converts_per_hour_with_every_row_exact(tmp_path):
    funding = make_funding(H0 + 3 * SECOND_NS, H0 + 2 * HOUR_NS - SECOND_NS)
    store = _runs(tmp_path, funding)

    _compactor(store).run_pass(_later())

    rows = _read(store, H0) + _read(store, H0 + HOUR_NS)
    # Deserialized rates come back as strings; Decimal(str()) is how the lab reads them.
    assert [(r.ts_init, Decimal(str(r.rate)), r.next_funding_ns) for r in rows] == [
        (f.ts_init, f.rate, f.next_funding_ns) for f in funding
    ]
    assert all(H0 <= r.ts_init < H0 + HOUR_NS for r in _read(store, H0))
    done = store.load_done()
    assert set(done.hours[stream_key(FUNDING_TABLE, INSTRUMENT)]) == {H0, H0 + HOUR_NS}


def test_rows_at_one_ns_keep_their_arrival_order(tmp_path):
    iid = InstrumentId.from_str(INSTRUMENT)
    ts = H0 + 10 * SECOND_NS
    funding = [
        FundingRateUpdate(iid, Decimal("0.0003"), ts, ts),
        FundingRateUpdate(iid, Decimal("0.0001"), ts, ts),
        FundingRateUpdate(iid, Decimal("0.0002"), ts, ts),
    ]
    store = _runs(tmp_path, funding)

    _compactor(store).run_pass(_later())

    assert [Decimal(str(r.rate)) for r in _read(store, H0)] == [Decimal("0.0003"), Decimal("0.0001"), Decimal("0.0002")]


def test_an_hour_without_funding_rows_writes_no_file(tmp_path):
    store = _runs(tmp_path, make_funding(H0 + 3 * SECOND_NS, H0 + 30 * 60 * SECOND_NS))

    _compactor(store).run_pass(_later())

    assert store.data_file(FUNDING_TABLE, INSTRUMENT, H0).is_file()
    assert not store.data_file(FUNDING_TABLE, INSTRUMENT, H0 + HOUR_NS).exists()


def test_a_rerun_changes_nothing(tmp_path):
    store = _runs(tmp_path, make_funding(H0 + 3 * SECOND_NS, H0 + 2 * HOUR_NS - SECOND_NS))
    _compactor(store).run_pass(_later())
    before = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")}
    done_before = store.done_path.read_text()

    _compactor(store).run_pass(_later())

    assert {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")} == before
    assert store.done_path.read_text() == done_before


def test_mark_and_index_convert_and_a_long_pause_is_a_silence_gap(tmp_path):
    """covers: spec 0009 AC-2, AC-7 (silence for mark and index, none for funding)."""
    from nautilus_trader.model.data import IndexPriceUpdate, MarkPriceUpdate

    from recorder.compactor.days import read_gaps
    from recorder.compactor.store import INDEX_TABLE, MARK_TABLE

    end = H0 + 2 * HOUR_NS - SECOND_NS
    pause = (H0 + 10 * 60 * SECOND_NS, H0 + 11 * 60 * SECOND_NS + 30 * SECOND_NS)  # 90 s, no rows
    short = (H0 + 20 * 60 * SECOND_NS, H0 + 20 * 60 * SECOND_NS + 30 * SECOND_NS)  # 30 s: no gap
    mark = make_prices(MarkPriceUpdate, H0 + 3 * SECOND_NS, end, pauses=[pause])
    index = make_prices(IndexPriceUpdate, H0 + 3 * SECOND_NS, end, pauses=[short])
    funding = make_funding(H0 + 3 * SECOND_NS, end, every_ns=600 * SECOND_NS)  # quiet: no silence for funding
    store = _runs(tmp_path, funding)
    rotate = [H0 + HOUR_NS, H0 + 2 * HOUR_NS]
    write_context(store.live, "a-dead", MARK_TABLE, mark, rotate_at=rotate)
    write_context(store.live, "a-dead", INDEX_TABLE, index, rotate_at=rotate)

    _compactor(store).run_pass(_later())

    rows = pq.read_table(store.data_file(MARK_TABLE, INSTRUMENT, H0)).to_pylist()
    rows += pq.read_table(store.data_file(MARK_TABLE, INSTRUMENT, H0 + HOUR_NS)).to_pylist()
    assert [r["ts_init"] for r in rows] == [m.ts_init for m in mark]
    before = max(m.ts_init for m in mark if m.ts_init < pause[0])
    after = min(m.ts_init for m in mark if m.ts_init >= pause[1])
    silences = [g for g in read_gaps(store, "2026-10-08", stream=None) if g.kind == "silence"]
    assert silences == [Gap(before, INSTRUMENT, "silence", after, stream=MARK_TABLE)]
    assert store.load_done().context_streams[stream_key(MARK_TABLE, INSTRUMENT)]["last_ts"] == mark[-1].ts_init


def _two_runs_with(tmp_path, a_rows, b_rows):
    """Two dead runs with books (a: 06:00 to 06:59:50, b: from 07:00:02) and their context rows by
    table, and a current run hours later."""
    store = CatalogStore(tmp_path)
    a_end = H0 + HOUR_NS - 10 * SECOND_NS
    b_start = H0 + HOUR_NS + 2 * SECOND_NS
    write_run(store.live, "a-dead", make_feed(H0 + 7 * SECOND_NS, a_end).messages, rotate_at=[])
    write_run(store.live, "b-dead", make_feed(b_start, H0 + 2 * HOUR_NS - SECOND_NS, seed=5).messages, rotate_at=[])
    for run, by_table in (("a-dead", a_rows), ("b-dead", b_rows)):
        for table, rows in by_table.items():
            write_context(store.live, run, table, rows, rotate_at=[])
    later = make_feed(H0 + 5 * HOUR_NS, H0 + 5 * HOUR_NS + 60 * SECOND_NS, seed=3)
    write_run(store.live, "z-current", later.messages, rotate_at=[])
    return store


def test_a_liquidation_resent_after_a_restart_across_the_hour_is_kept_once(tmp_path, caplog):
    """covers: spec 0009 AC-2, AC-5."""
    import logging

    from recorder.compactor.store import LIQUIDATION_TABLE
    from trading_core.node.context_types import LiquidationTick

    a = [
        liquidation(H0 + 100 * SECOND_NS, "x"),
        liquidation(H0 + 3_590 * SECOND_NS, "y"),
        liquidation(H0 + 3_590 * SECOND_NS, "y"),
    ]
    b = [liquidation(H0 + HOUR_NS + 3 * SECOND_NS, "y"), liquidation(H0 + HOUR_NS + 4 * SECOND_NS, "z", side="BUY")]
    store = _two_runs_with(tmp_path, {LIQUIDATION_TABLE: a}, {LIQUIDATION_TABLE: b})

    with caplog.at_level(logging.WARNING):
        _compactor(store).run_pass(_later())

    def ids(hour):
        table = pq.read_table(store.data_file(LIQUIDATION_TABLE, INSTRUMENT, hour))
        return [(t.trade_id, t.ts_init) for t in ArrowSerializer.deserialize(LiquidationTick, table)]

    assert ids(H0) == [("x", a[0].ts_init), ("y", a[1].ts_init)]  # the earliest copy kept
    assert ids(H0 + HOUR_NS) == [("z", b[1].ts_init)]
    logged = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == "liquidation_duplicate"]
    assert [f["count"] for f in logged] == [1, 1]
    key = stream_key(LIQUIDATION_TABLE, INSTRUMENT)
    assert store.load_done().liquidation_uids[key] == {"hour": H0 + HOUR_NS, "uids": ["z"]}
    before = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")}

    _compactor(store).run_pass(_later())

    assert {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")} == before


def test_an_outage_line_is_a_reconnect_gap_of_both_raw_streams_and_beats_a_silence(tmp_path):
    """covers: spec 0009 AC-6, AC-7."""
    from recorder.compactor.days import read_gaps
    from recorder.compactor.store import LIQUIDATION_TABLE, OPEN_INTEREST_TABLE
    from trading_core.marketdata.context_feed import Outage

    pause = (H0 + 600 * SECOND_NS, H0 + 700 * SECOND_NS)
    oi = make_open_interest(H0 + 3 * SECOND_NS, H0 + HOUR_NS - 20 * SECOND_NS, pauses=[pause])
    store = _two_runs_with(tmp_path, {OPEN_INTEREST_TABLE: oi}, {})
    before = max(r.ts_init for r in oi if r.ts_init < pause[0])
    after = min(r.ts_init for r in oi if r.ts_init >= pause[1])
    outage = Outage(before, after, "closed")
    store.outages_file("a-dead").write_text(outage.line())

    _compactor(store).run_pass(_later())

    gaps = [
        g for g in read_gaps(store, "2026-10-08", stream=None) if g.stream in (OPEN_INTEREST_TABLE, LIQUIDATION_TABLE)
    ]
    reconnects = [g for g in gaps if g.kind == "reconnect"]
    assert sorted(reconnects) == sorted(
        [Gap(before, INSTRUMENT, "reconnect", after, stream=s) for s in (OPEN_INTEREST_TABLE, LIQUIDATION_TABLE)]
    )
    assert not [g for g in gaps if g.kind == "silence"]  # the same span's silence lost to the reconnect
    assert store.load_done().outages == {"a-dead": 1}
    rows = pq.read_table(store.data_file(OPEN_INTEREST_TABLE, INSTRUMENT, H0)).column("value").to_pylist()
    assert rows == [str(r.value) for r in oi]


def test_a_restart_is_one_recorder_down_gap_per_context_stream_from_the_book_runs(tmp_path):
    """covers: spec 0009 AC-7 (recorder_down from the book's run boundaries; a quiet liquidation stream
    gets no gap of its own)."""
    from recorder.compactor.days import read_gaps
    from recorder.compactor.store import CONTEXT_TABLES

    a = {FUNDING_TABLE: make_funding(H0 + 3 * SECOND_NS, H0 + HOUR_NS - 20 * SECOND_NS)}
    b = {FUNDING_TABLE: make_funding(H0 + HOUR_NS + 5 * SECOND_NS, H0 + 2 * HOUR_NS - SECOND_NS)}
    store = _two_runs_with(tmp_path, a, b)

    _compactor(store).run_pass(_later())

    (book,) = read_gaps(store, "2026-10-08")
    assert book.kind == "recorder_down"
    context = [g for g in read_gaps(store, "2026-10-08", stream=None) if g.stream in CONTEXT_TABLES]
    assert sorted(context) == sorted(
        Gap(book.start_ns, INSTRUMENT, "recorder_down", book.end_ns, stream=s) for s in CONTEXT_TABLES
    )


def test_a_funding_row_later_than_the_window_goes_to_quarantine(tmp_path):
    """covers: spec 0009 AC-2 (late context rows are quarantined and counted, as for trades)."""
    from recorder.compactor import days
    from trading_core.marketdata.hours import day_of

    funding = make_funding(H0 + 3 * SECOND_NS, H0 + 2 * HOUR_NS - SECOND_NS)
    i = next(n for n, f in enumerate(funding) if f.ts_init >= H0 + 30 * 60 * SECOND_NS)
    late = funding[i]
    after = next(n for n, f in enumerate(funding) if f.ts_init > late.ts_init + 700 * SECOND_NS)
    arrival = funding[:i] + funding[i + 1 : after] + [late] + funding[after:]
    store = _runs(tmp_path, arrival)

    _compactor(store).run_pass(_later())

    assert [r.ts_init for r in _read(store, H0)] == [
        f.ts_init for f in funding if f.ts_init < H0 + HOUR_NS and f is not late
    ]
    quarantine = pq.read_table(str(store.quarantine_file(FUNDING_TABLE, INSTRUMENT, H0)))
    assert quarantine.column("ts_init").to_pylist() == [late.ts_init]
    done = store.load_done()
    assert done.incomplete[stream_key(FUNDING_TABLE, INSTRUMENT)] == {H0: 1}
    manifest = days._manifest(store, done, day_of(H0))
    (stream,) = [s for s in manifest["streams"] if s["type"] == FUNDING_TABLE]
    assert (stream["late_rows"], stream["incomplete_hours"]) == (1, [H0])
