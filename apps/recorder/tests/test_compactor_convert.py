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
    """A dead run holding the feed (rotating after each hour), and an empty current run."""
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, end_ns)
    write_run(store.live, "a-dead", feed.messages, rotate_at=[H0 + HOUR_NS, H0 + 2 * HOUR_NS])
    time.sleep(0.01)
    (store.live / "b-current").mkdir()
    return store, feed


def _top(levels_bids, levels_asks):
    return (
        [(str(p), str(s)) for p, s in levels_bids[:TOP]],
        [(str(p), str(s)) for p, s in levels_asks[:TOP]],
    )


def _first(level):
    order = level.first()
    assert order is not None
    return order


def _reference_samples(feed, sample_ts):
    """Replay the raw feed from the start and take the book at each sample time."""
    book = p3.OrderBook(p3.InstrumentId.from_str(INSTRUMENT), p3.BookType.L2_MBP)
    out = {}
    for message in feed.messages:
        for delta in message:
            book.apply_delta(delta)
        ts = message[0].ts_init
        if ts in sample_ts:
            out[ts] = _top(
                [(lv.price, _first(lv).size) for lv in book.bids()],
                [(lv.price, _first(lv).size) for lv in book.asks()],
            )
    return out


class _Sampler(Actor):  # type: ignore[misc]
    """Takes the engine's book at the first F_LAST after every 60 s of data, and at the last row."""

    def __init__(self, instrument_id, start_ns):
        super().__init__()
        self._id = instrument_id
        self._next = start_ns + SAMPLE_NS
        self.samples = {}
        self.last_ts = None

    def on_start(self):
        self.subscribe_order_book_deltas(self._id, BookType.L2_MBP)

    def on_order_book_deltas(self, deltas):
        last = deltas.deltas[-1]
        if not last.flags & F_LAST:
            return
        self.last_ts = last.ts_init
        book = self.cache.order_book(self._id)
        top = _top(
            [(lv.price, lv.orders()[0].size) for lv in book.bids()],
            [(lv.price, lv.orders()[0].size) for lv in book.asks()],
        )
        self.samples["last"] = (last.ts_init, top)
        if last.ts_init >= self._next:
            self.samples[last.ts_init] = top
            while self._next <= last.ts_init:
                self._next += SAMPLE_NS


def test_hours_convert_with_a_snapshot_and_the_second_hour_replays_alone(tmp_path):
    store, feed = _two_runs(tmp_path, H0 + 2 * HOUR_NS + 20 * 60 * SECOND_NS)
    compactor = Compactor(store, grace_ns=0)
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
    sampler.samples[last_ts] = last_top
    assert len(sampler.samples) >= 55
    reference = _reference_samples(feed, set(sampler.samples))
    assert reference.keys() == sampler.samples.keys()
    for ts, top in sampler.samples.items():
        assert top == reference[ts], f"book differs at {ts}"


def test_a_rerun_changes_nothing(tmp_path):
    store, _ = _two_runs(tmp_path, H0 + HOUR_NS + 30 * 60 * SECOND_NS)
    Compactor(store, grace_ns=0).run_pass(_later())
    before = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")}
    done_before = store.done_path.read_text()

    Compactor(store, grace_ns=0).run_pass(_later())

    assert {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.parquet")} == before
    assert store.done_path.read_text() == done_before


def test_the_open_hour_waits_while_the_current_run_may_hold_more_of_it(tmp_path):
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, H0 + HOUR_NS + 10 * 60 * SECOND_NS)
    # One run that is still current: its newest file is open, so only the first hour can finish.
    write_run(store.live, "a-current", feed.messages, rotate_at=[H0 + HOUR_NS])
    Compactor(store, grace_ns=0).run_pass(_later())
    done = json.loads(store.done_path.read_text())
    assert [int(h) for h in done["hours"][KEY]] == [H0]


def test_a_row_count_mismatch_keeps_nothing(tmp_path, monkeypatch):
    store, _ = _two_runs(tmp_path, H0 + HOUR_NS + 5 * 60 * SECOND_NS)
    real_commit = convert.HourSink.commit
    monkeypatch.setattr(convert.HourSink, "commit", lambda self, expected: real_commit(self, expected + 1))
    with pytest.raises(HourCountMismatch):
        Compactor(store, grace_ns=0).run_pass(_later())
    assert not list((tmp_path / "data").rglob("*.parquet"))
    assert not list((tmp_path / "data").rglob("*.tmp"))
    assert not store.done_path.exists()
