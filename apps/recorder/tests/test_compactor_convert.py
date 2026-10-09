"""covers: spec 0007 AC-2 (hourly Parquet, atomic, row count checked) and AC-3 (hour start snapshot, replay)."""

import json
import time

import pyarrow.parquet as pq
import pytest
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, make_feed, write_run
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from recorder.compactor import convert
from recorder.compactor.convert import F_LAST, F_SNAPSHOT
from recorder.compactor.main import Compactor
from recorder.compactor.store import DELTAS_TABLE, CatalogStore, stream_key
from trading_core.marketdata.errors import HourCountMismatch

KEY = stream_key(DELTAS_TABLE, INSTRUMENT)
SAMPLE_NS = 60 * SECOND_NS
TOP = 20


def _later():
    # Every file is past the grace and every hour long over.
    return time.time_ns() + 10 * HOUR_NS


def _two_runs(tmp_path, end_ns):
    """A dead run holding the feed (rotating after each hour), and a current run hours later."""
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, end_ns)
    write_run(store.live, "a-dead", feed.messages, rotate_at=[H0 + HOUR_NS, H0 + 2 * HOUR_NS])
    later = make_feed(H0 + 5 * HOUR_NS, H0 + 5 * HOUR_NS + 60 * SECOND_NS, seed=3)
    write_run(store.live, "b-current", later.messages, rotate_at=[])
    return store, feed


def _compactor(store):
    return Compactor(store, grace_ns=0, floor_bytes=0)


def _top(levels_bids, levels_asks):
    return (
        [(str(p), str(s)) for p, s in levels_bids[:TOP]],
        [(str(p), str(s)) for p, s in levels_asks[:TOP]],
    )


def _first(level):
    order = level.first()
    assert order is not None
    return order


def _reference_samples(feed, sample_ts, *, whole_messages=False):
    """Replay the raw feed from the start; at each sample time, the book after the first row with
    that ``ts_init`` (or after every message up to it, for the hour start's snapshot)."""
    book = p3.OrderBook(p3.InstrumentId.from_str(INSTRUMENT), p3.BookType.L2_MBP)
    out = {}
    pending = sorted(sample_ts)

    def take(ts):
        out[ts] = _top(
            [(lv.price, _first(lv).size) for lv in book.bids()],
            [(lv.price, _first(lv).size) for lv in book.asks()],
        )

    for message in feed.messages:
        ts = message[0].ts_init
        while pending and pending[0] < ts:
            take(pending.pop(0))
        for i, delta in enumerate(message):
            book.apply_delta(delta)
            if i == 0 and not whole_messages and pending and pending[0] == ts:
                take(pending.pop(0))
    for ts in pending:
        take(ts)
    return out


class _Sampler(Actor):  # type: ignore[misc]
    """Takes the engine's book at the first row at or after H + k * 60 s (k = 1 to 59), and at the
    last row (AC-3). Kraken rows carry no ``F_LAST``, so the sample point is a row, not a flag; the
    engine passes this actor one row at a time.

    k = 0 is checked on the snapshot rows directly: the hour's first rows are the synthetic snapshot,
    which the raw feed does not have.
    """

    def __init__(self, instrument_id, start_ns):
        super().__init__()
        self._id = instrument_id
        self._hour_start = start_ns
        self._k = 1
        self.samples = {}

    def on_start(self):
        self.subscribe_order_book_deltas(self._id, BookType.L2_MBP)

    def on_order_book_deltas(self, deltas):
        ts = deltas.deltas[-1].ts_init
        book = self.cache.order_book(self._id)
        top = _top(
            [(lv.price, lv.orders()[0].size) for lv in book.bids()],
            [(lv.price, lv.orders()[0].size) for lv in book.asks()],
        )
        self.samples["last"] = (ts, top)
        if self._k < 60 and ts >= self._hour_start + self._k * SAMPLE_NS:
            self.samples[ts] = top
            while self._k < 60 and self._hour_start + self._k * SAMPLE_NS <= ts:
                self._k += 1


def test_hours_convert_with_a_snapshot_and_the_second_hour_replays_alone(tmp_path):
    store, feed = _two_runs(tmp_path, H0 + 2 * HOUR_NS + 20 * 60 * SECOND_NS)
    compactor = _compactor(store)
    compactor.run_pass(_later())

    done = json.loads(store.done_path.read_text())
    hours = {int(h): v for h, v in done["hours"][KEY].items()}
    assert sorted(hours) == [H0, H0 + HOUR_NS, H0 + 2 * HOUR_NS]
    assert hours[H0]["snapshot_rows"] == 0  # no carry before the first hour
    assert hours[H0 + HOUR_NS]["snapshot_rows"] > 0
    for hour, stats in hours.items():
        path = store.data_file(DELTAS_TABLE, INSTRUMENT, hour)
        assert pq.ParquetFile(str(path)).metadata.num_rows == stats["rows"]
        assert not list(path.parent.glob("*.tmp"))
    # Source rows plus snapshot rows, nothing lost or doubled.
    raw = sum(len(m) for m in feed.messages)
    assert sum(v["rows"] - v["snapshot_rows"] for v in hours.values()) == raw

    # The second hour opens with the snapshot: CLEAR, then the levels, ts = the hour, F_LAST at the end.
    table = pq.read_table(str(store.data_file(DELTAS_TABLE, INSTRUMENT, H0 + HOUR_NS)))
    snap = table.slice(0, hours[H0 + HOUR_NS]["snapshot_rows"]).to_pydict()
    assert snap["action"][0] == convert.CLEAR
    assert set(snap["ts_init"]) == {H0 + HOUR_NS}
    assert all(f & F_SNAPSHOT for f in snap["flags"])
    assert snap["flags"][-1] & F_LAST
    assert set(snap["order_id"]) == {0}

    # AC-3: replay hour two alone through the catalog and a BacktestEngine.
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_data([instrument])
    deltas = catalog.order_book_deltas(instrument_ids=[INSTRUMENT], start=H0 + HOUR_NS, end=H0 + 2 * HOUR_NS - 1)
    assert len(deltas) == hours[H0 + HOUR_NS]["rows"]
    engine = BacktestEngine(BacktestEngineConfig(trader_id="TESTER-001"))
    engine.add_venue(
        Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000, USDT)],
        book_type=BookType.L2_MBP,
    )
    engine.add_instrument(instrument)
    engine.add_data(deltas)
    sampler = _Sampler(InstrumentId.from_str(INSTRUMENT), H0 + HOUR_NS)
    engine.add_actor(sampler)
    engine.run()
    engine.dispose()

    last_ts, last_top = sampler.samples.pop("last")
    assert len(sampler.samples) >= 59
    # The hour's last row ends a message, so its book is the one after that whole message.
    assert last_top == _reference_samples(feed, {last_ts}, whole_messages=True)[last_ts]
    # k = 0 is the snapshot itself: its rows alone rebuild the raw feed's book at the hour start.
    snapshot = convert.BookCarry(INSTRUMENT)
    snapshot.restore(table.slice(0, hours[H0 + HOUR_NS]["snapshot_rows"]), H0 + HOUR_NS)
    assert (
        _top(
            [(lv.price, _first(lv).size) for lv in snapshot.book.bids()],
            [(lv.price, _first(lv).size) for lv in snapshot.book.asks()],
        )
        == _reference_samples(feed, {H0 + HOUR_NS}, whole_messages=True)[H0 + HOUR_NS]
    )
    reference = _reference_samples(feed, set(sampler.samples))
    assert reference.keys() == sampler.samples.keys()
    for ts, top in sampler.samples.items():
        assert top == reference[ts], f"book differs at {ts}"


def test_a_rerun_changes_nothing(tmp_path):
    store, _ = _two_runs(tmp_path, H0 + HOUR_NS + 30 * 60 * SECOND_NS)
    _compactor(store).run_pass(_later())
    before = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")}
    done_before = store.done_path.read_text()

    _compactor(store).run_pass(_later())

    assert {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")} == before
    assert store.done_path.read_text() == done_before


def test_the_open_hour_waits_until_the_watermark_has_passed_its_end(tmp_path):
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, H0 + HOUR_NS + 30 * 60 * SECOND_NS)
    # One run that is still current: its newest file is open and never read. The closed files end
    # at H0 + 1 h 5 min, so the watermark (10 minutes behind) has not passed H0 + 1 h.
    write_run(store.live, "a-current", feed.messages, rotate_at=[H0 + HOUR_NS, H0 + HOUR_NS + 5 * 60 * SECOND_NS])
    compactor = _compactor(store)
    compactor.run_pass(_later())
    assert not store.done_path.exists() or not json.loads(store.done_path.read_text())["hours"]
    # A newer file closes the one that runs to H0 + 1 h 30 min: the watermark passes H0 + 1 h.
    write_run(store.live, "a-current", make_feed(H0 + 2 * HOUR_NS, H0 + 2 * HOUR_NS + SECOND_NS).messages, [])
    compactor.run_pass(_later())
    done = json.loads(store.done_path.read_text())
    assert [int(h) for h in done["hours"][KEY]] == [H0]


def test_a_row_count_mismatch_keeps_nothing(tmp_path, monkeypatch):
    store, _ = _two_runs(tmp_path, H0 + HOUR_NS + 5 * 60 * SECOND_NS)
    real_commit = convert.HourSink.commit
    monkeypatch.setattr(convert.HourSink, "commit", lambda self, expected: real_commit(self, expected + 1))
    with pytest.raises(HourCountMismatch):
        _compactor(store).run_pass(_later())
    assert not list((tmp_path / "data").rglob("*.parquet"))
    assert not list((tmp_path / "data").rglob("*.tmp"))
    assert not store.load_done().hours
    assert list((store.live / "a-dead").rglob("*.feather"))  # the feather stays
