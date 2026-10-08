"""covers: spec 0007 AC-2 (all types, feather deletion, closed files), AC-9 (space gate), AC-11 (crash and
rerun), AC-12 (overlap)."""

import json
import logging
import shutil
import time
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from feed_fixture import (
    H0,
    HOUR_NS,
    INSTRUMENT,
    SECOND_NS,
    make_feed,
    make_trades,
    write_instruments,
    write_run,
    write_trades,
)
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from recorder.compactor import convert
from recorder.compactor.main import Compactor
from recorder.compactor.store import (
    DELTAS_TABLE,
    INSTRUMENTS_TABLE,
    TRADES_TABLE,
    CatalogStore,
    atomic_write,
    stream_key,
)

MINUTE_NS = 60 * SECOND_NS
DELTAS = stream_key(DELTAS_TABLE, INSTRUMENT)
TRADES = stream_key(TRADES_TABLE, INSTRUMENT)
ROTATIONS = [H0 + HOUR_NS, H0 + 2 * HOUR_NS]


def _later():
    return time.time_ns() + 10 * HOUR_NS


def _compactor(store, **kwargs):
    return Compactor(store, grace_ns=0, floor_bytes=0, **kwargs)


def _hours(store, key):
    return sorted(store.load_done().hours.get(key, {}))


def _full_run(store, run="a-dead", start=H0 + 7 * SECOND_NS, end=H0 + 2 * HOUR_NS + 20 * MINUTE_NS):
    """A run with deltas, trades, and an instrument file holding ours and another venue instrument."""
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    write_instruments(store.live, run, [btc, eth], created_ns=start - SECOND_NS)
    feed = make_feed(start, end)
    write_run(store.live, run, feed.messages, rotate_at=ROTATIONS)
    trades = make_trades(start, end)
    write_trades(store.live, run, trades, rotate_at=ROTATIONS)
    return feed, trades


def _current_run(store, start=H0 + 6 * HOUR_NS):
    later = make_feed(start, start + MINUTE_NS, seed=3)
    write_run(store.live, "z-current", later.messages, rotate_at=[])


def test_deltas_trades_and_instruments_convert_and_the_dead_run_is_deleted(tmp_path):
    store = CatalogStore(tmp_path)
    _, trades = _full_run(store)
    _current_run(store)
    _compactor(store).run_pass(_later())

    hours = [H0, H0 + HOUR_NS, H0 + 2 * HOUR_NS]
    assert _hours(store, DELTAS) == hours
    assert _hours(store, TRADES) == hours
    assert _hours(store, stream_key(INSTRUMENTS_TABLE, INSTRUMENT)) == hours
    catalog = ParquetDataCatalog(str(tmp_path))
    assert len(catalog.trade_ticks(instrument_ids=[INSTRUMENT])) == len(trades)
    # One definition row per hour, only for the instrument we record.
    assert {i.id.value for i in catalog.instruments()} == {INSTRUMENT}
    one_hour = pq.read_table(str(store.data_file(INSTRUMENTS_TABLE, INSTRUMENT, H0 + HOUR_NS)))
    assert one_hour.column("ts_init").to_pylist() == [H0 + HOUR_NS]
    # Every feather of the dead run is converted, so the run folder is gone; the current one stays.
    assert not (store.live / "a-dead").exists()
    assert (store.live / "z-current").exists()
    assert store.load_done().files == set()


def test_the_current_runs_newest_files_are_never_read_however_quiet(tmp_path):
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, H0 + 20 * MINUTE_NS)
    write_run(store.live, "a-current", feed.messages, rotate_at=[])
    trades = make_trades(H0, H0 + 5 * MINUTE_NS)  # then quiet for hours
    write_trades(store.live, "a-current", trades, rotate_at=[])
    _compactor(store).run_pass(_later())
    assert store.load_done().hours == {}
    assert len(list(store.live.rglob("*.feather"))) == 2


def test_a_restart_inside_an_hour_appends_both_runs_and_the_next_hour_starts_from_the_new_feed(tmp_path):
    store = CatalogStore(tmp_path)
    first = make_feed(H0 + 7 * SECOND_NS, H0 + 30 * MINUTE_NS)
    write_run(store.live, "a-dead", first.messages, rotate_at=[])
    # The recorder comes back 40 s later; its feed opens with a CLEAR snapshot again.
    second = make_feed(H0 + 30 * MINUTE_NS + 40 * SECOND_NS, H0 + HOUR_NS + 30 * MINUTE_NS, seed=9)
    write_run(store.live, "b-dead", second.messages, rotate_at=[H0 + HOUR_NS])
    _current_run(store)
    _compactor(store).run_pass(_later())

    done = store.load_done()
    stats = done.hours[DELTAS]
    rows_first = sum(len(m) for m in first.messages)
    rows_second_h0 = sum(len(m) for m in second.messages if m[0].ts_init < H0 + HOUR_NS)
    assert stats[H0]["rows"] == rows_first + rows_second_h0
    ts = pq.read_table(str(store.data_file(DELTAS_TABLE, INSTRUMENT, H0))).column("ts_init").to_numpy()
    assert (ts[1:] >= ts[:-1]).all()
    # The new run's own CLEAR made the book valid again, so the next hour gets a snapshot.
    assert stats[H0 + HOUR_NS]["snapshot_rows"] > 0


def test_overlapping_runs_write_nothing_for_the_hour_and_keep_the_feather(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    first = make_feed(H0 + 7 * SECOND_NS, H0 + 40 * MINUTE_NS)
    write_run(store.live, "a-dead", first.messages, rotate_at=[])
    # A second recorder writing at the same time: its rows go back before the first run's last row.
    second = make_feed(H0 + 20 * MINUTE_NS, H0 + HOUR_NS + 20 * MINUTE_NS, seed=5)
    write_run(store.live, "b-dead", second.messages, rotate_at=[H0 + HOUR_NS])
    _current_run(store)
    with caplog.at_level(logging.ERROR):
        _compactor(store).run_pass(_later())

    assert [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "catalog_hour_overlap"]
    done = store.load_done()
    assert H0 in done.overlaps[DELTAS]
    assert not store.data_file(DELTAS_TABLE, INSTRUMENT, H0).exists()
    for rel in done.kept:
        assert (tmp_path / rel).exists()
    # The next hour still converts, but with no synthetic snapshot (no CLEAR came after the overlap).
    assert done.hours[DELTAS][H0 + HOUR_NS]["snapshot_rows"] == 0
    assert done.carry[INSTRUMENT] is None


def _snapshot(root):
    """The catalog files (data, gaps, manifests) and done.json, to compare two catalogs."""
    files = {
        str(p.relative_to(root)): p.read_bytes()
        for folder in ("data", "gaps", "manifests")
        for p in (root / folder).rglob("*")
        if p.is_file()
    }
    return files, json.loads((root / ".compactor" / "done.json").read_text())


@pytest.mark.parametrize("step", ["after_rename", "after_book", "after_done", "during_delete"])
def test_a_pass_killed_at_any_commit_step_reruns_to_the_same_files(tmp_path, monkeypatch, step):
    clean, crashed = tmp_path / "clean", tmp_path / "crashed"
    store = CatalogStore(clean)
    _full_run(store)
    _current_run(store)
    shutil.copytree(clean, crashed)
    _compactor(store).run_pass(_later())

    crashed_store = CatalogStore(crashed)
    calls = {"n": 0}

    class Killed(Exception):
        pass

    def kill_on(n):
        calls["n"] += 1
        if calls["n"] == n:
            raise Killed

    if step == "after_rename":  # the hour's Parquet is in place, its book not yet saved
        real = atomic_write
        monkeypatch.setattr(convert, "atomic_write", lambda *a: (kill_on(2), real(*a)))
    elif step == "after_book":  # the book is saved, done.json not yet rewritten
        real_save = crashed_store.save_done
        monkeypatch.setattr(crashed_store, "save_done", lambda d: (kill_on(3), real_save(d)))
    elif step == "after_done":  # the hour is committed, the pass dies before the next one
        real_finish = convert.HourStream._finish
        monkeypatch.setattr(convert.HourStream, "_finish", lambda self, d: (real_finish(self, d), kill_on(2)))
    else:  # some feather is deleted, the rest not, done.json still lists it
        real_unlink = Path.unlink
        monkeypatch.setattr(
            Path, "unlink", lambda self, missing_ok=False: (kill_on(3), real_unlink(self, missing_ok=missing_ok))
        )
    killed = _compactor(crashed_store)
    with pytest.raises(Killed):
        killed.run_pass(_later())
    killed.abort()  # as main() does after a failed pass
    monkeypatch.undo()
    # A killed process also leaves its open hour's temp file behind.
    leftover = crashed / "data" / DELTAS_TABLE / INSTRUMENT / "left.parquet.tmp"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes(b"half written")

    _compactor(CatalogStore(crashed)).run_pass(_later())

    assert not list(crashed.rglob("*.tmp"))
    assert _snapshot(crashed) == _snapshot(clean)


def test_a_dead_run_waits_for_space_and_holds_back_newer_hours(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    _full_run(store)
    _current_run(store)
    with caplog.at_level(logging.ERROR):
        Compactor(store, grace_ns=0, floor_bytes=10**18).run_pass(_later())
    blocked = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "backlog_needs_space"]
    assert blocked
    assert blocked[0].fields["run"] == "a-dead"
    assert store.load_done().hours == {}

    _compactor(store).run_pass(_later())
    assert _hours(store, DELTAS) == [H0, H0 + HOUR_NS, H0 + 2 * HOUR_NS]
