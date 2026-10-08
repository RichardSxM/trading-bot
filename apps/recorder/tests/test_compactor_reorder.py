"""covers: spec 0007 AC-12 (reorder window, late rows to quarantine, ready rule, run end, row cap), AC-2 (count
check with late rows), AC-4 (gaps from written rows), AC-5 (late counts in the manifest), AC-11 (reruns with
late rows and held rows)."""

import json
import logging
import shutil
import time

import pyarrow.parquet as pq
import pytest
from feed_fixture import (
    H0,
    HOUR_NS,
    INSTRUMENT,
    SECOND_NS,
    delay,
    make_feed,
    make_trades,
    message_at,
    shuffle_within,
    write_run,
    write_trades,
)
from pydantic import ValidationError

from recorder.compactor import convert, days
from recorder.compactor.main import Compactor, CompactorSettings
from recorder.compactor.store import DELTAS_TABLE, TRADES_TABLE, CatalogStore, stream_key
from trading_core.marketdata.hours import day_of

MINUTE_NS = 60 * SECOND_NS
DELTAS = stream_key(DELTAS_TABLE, INSTRUMENT)
TRADES = stream_key(TRADES_TABLE, INSTRUMENT)
ROTATIONS = [H0 + HOUR_NS, H0 + 2 * HOUR_NS]
START, END = H0 + 7 * SECOND_NS, H0 + 2 * HOUR_NS + 20 * MINUTE_NS


def _later():
    return time.time_ns() + 10 * HOUR_NS


def _compactor(store, **kwargs):
    return Compactor(store, grace_ns=0, floor_bytes=0, **kwargs)


def _codes(caplog, code):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == code]


def _files(root, *folders):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for folder in folders
        for p in (root / folder).rglob("*")
        if p.is_file() and p.suffix != ".tmp"  # an open hour is unfinished
    }


def _catalog(root, messages, rotate_at=ROTATIONS, *, current=True):
    """A dead run of ``messages`` (in arrival order) and, far later, a current run."""
    store = CatalogStore(root)
    write_run(store.live, "a-dead", messages, rotate_at=rotate_at)
    if current:
        later = make_feed(H0 + 6 * HOUR_NS, H0 + 6 * HOUR_NS + MINUTE_NS, seed=3)
        write_run(store.live, "z-current", later.messages, rotate_at=[])
    return store


def test_rows_shuffled_within_the_window_convert_exactly_like_the_sorted_stream(tmp_path, caplog):
    feed = make_feed(START, END)
    sorted_store = _catalog(tmp_path / "sorted", feed.messages)
    shuffled_store = _catalog(tmp_path / "shuffled", shuffle_within(feed.messages, 590 * SECOND_NS))
    _compactor(sorted_store).run_pass(_later())
    with caplog.at_level(logging.WARNING):
        _compactor(shuffled_store).run_pass(_later())

    assert _files(sorted_store.root, "data", "gaps") == _files(shuffled_store.root, "data", "gaps")
    assert _files(sorted_store.root, ".compactor/books") == _files(shuffled_store.root, ".compactor/books")
    a, b = sorted_store.load_done(), shuffled_store.load_done()
    assert (a.hours, a.carry, a.streams) == (b.hours, b.carry, b.streams)
    assert b.incomplete == {}
    assert not (shuffled_store.root / "quarantine").exists()
    reordered = _codes(caplog, "catalog_hour_reordered")
    assert {f["hour_ns"] for f in reordered if f["stream"] == DELTAS} == {H0, H0 + HOUR_NS, H0 + 2 * HOUR_NS}
    assert all(f["max_late_s"] < 590 for f in reordered)


def test_a_row_500_s_late_across_the_hour_boundary_lands_in_its_own_hour(tmp_path):
    feed = make_feed(START, END)
    late = delay(feed.messages, message_at(feed.messages, H0 + HOUR_NS - 10 * SECOND_NS), 500 * SECOND_NS)
    sorted_store = _catalog(tmp_path / "sorted", feed.messages)
    late_store = _catalog(tmp_path / "late", late)
    _compactor(sorted_store).run_pass(_later())
    _compactor(late_store).run_pass(_later())
    assert _files(sorted_store.root, "data") == _files(late_store.root, "data")
    assert late_store.load_done().incomplete == {}


def test_a_group_later_than_the_window_goes_to_quarantine_and_the_hour_converts_without_it(tmp_path, caplog):
    feed = make_feed(START, END)
    index = message_at(feed.messages, H0 + 30 * MINUTE_NS)
    group = feed.messages[index]
    store = _catalog(tmp_path, delay(feed.messages, index, 700 * SECOND_NS))
    with caplog.at_level(logging.ERROR):
        _compactor(store).run_pass(_later())

    late = _codes(caplog, "catalog_late_rows")
    assert [(f["hour_ns"], f["rows"], f["cause"]) for f in late] == [(H0, len(group), "window")]
    assert 699 < late[0]["max_late_s"] <= 700
    assert not _codes(caplog, "catalog_hour_mismatch")
    quarantine = pq.read_table(str(store.quarantine_file(DELTAS_TABLE, INSTRUMENT, H0)))
    assert quarantine.column("ts_init").to_pylist() == [group[0].ts_init] * len(group)
    done = store.load_done()
    assert done.incomplete[DELTAS] == {H0: len(group)}
    total = sum(len(m) for m in feed.messages)
    written = sum(s["rows"] - s["snapshot_rows"] for s in done.hours[DELTAS].values())
    assert written == total - len(group)
    # The book missed the late delta, so the next hour has no synthetic snapshot.
    assert done.hours[DELTAS][H0 + HOUR_NS]["snapshot_rows"] == 0
    manifest = days._manifest(store, done, day_of(H0))
    [deltas] = [s for s in manifest["streams"] if s["type"] == DELTAS_TABLE]
    assert (deltas["late_rows"], deltas["incomplete_hours"]) == (len(group), [H0])


def test_a_late_row_of_a_committed_hour_goes_to_quarantine_and_marks_the_next_hour_too(tmp_path):
    feed = make_feed(START, END)
    index = message_at(feed.messages, H0 + 59 * MINUTE_NS)
    group = feed.messages[index]
    rotate_at = [H0 + HOUR_NS, H0 + HOUR_NS + 15 * MINUTE_NS, H0 + 2 * HOUR_NS]
    without = feed.messages[:index] + feed.messages[index + 1 :]
    # The group arrives just after the rotation at H0 + 1 h 15 min, so in a later file than the
    # one that let H0 finish: H0 is in done.json and H0 + 1 h is open when it is read.
    later = delay(feed.messages, index, 16 * MINUTE_NS + 2 * SECOND_NS)
    clean_store = _catalog(tmp_path / "clean", without, rotate_at)
    late_store = _catalog(tmp_path / "late", later, rotate_at)
    _compactor(clean_store).run_pass(_later())
    _compactor(late_store).run_pass(_later())

    h0 = str(late_store.data_file(DELTAS_TABLE, INSTRUMENT, H0).relative_to(late_store.root))
    assert _files(late_store.root, "data")[h0] == _files(clean_store.root, "data")[h0]
    done = late_store.load_done()
    assert done.incomplete[DELTAS] == {H0: len(group), H0 + HOUR_NS: 0}
    assert done.hours[DELTAS][H0 + 2 * HOUR_NS]["snapshot_rows"] == 0


def test_chunk_size_never_changes_the_bytes(tmp_path, monkeypatch):
    feed = make_feed(H0 + 45 * MINUTE_NS, H0 + HOUR_NS + 20 * MINUTE_NS)
    shuffled = shuffle_within(feed.messages, 300 * SECOND_NS, seed=4)
    big = _catalog(tmp_path / "big", shuffled, rotate_at=[H0 + HOUR_NS])
    small = _catalog(tmp_path / "small", shuffled, rotate_at=[H0 + HOUR_NS])
    _compactor(big).run_pass(_later())
    monkeypatch.setattr(convert, "CHUNK_ROWS", 1)
    _compactor(small).run_pass(_later())
    assert _files(big.root, "data", "gaps") == _files(small.root, "data", "gaps")


@pytest.mark.parametrize("restart", [False, True])
def test_a_pass_boundary_with_rows_held_gives_the_same_result_as_one_pass(tmp_path, restart, caplog):
    feed = make_feed(START, END)
    shuffled = shuffle_within(feed.messages, 400 * SECOND_NS, seed=8)
    rotate_at = [H0 + k * 20 * MINUTE_NS for k in range(1, 8)]
    one = _catalog(tmp_path / "one", shuffled, rotate_at, current=False)
    _compactor(one).run_pass(_later())

    split = _catalog(tmp_path / "split", shuffled, rotate_at, current=False)
    files = sorted((split.live / "a-dead").rglob("*.feather"))
    hidden = [f.rename(f.with_suffix(".hidden")) for f in files[3:]]  # file 3 is open, 1 and 2 closed
    compactor = _compactor(split)
    compactor.run_pass(_later())
    assert compactor.streams[(DELTAS_TABLE, INSTRUMENT)].window.held_rows > 0
    for path in hidden:
        path.rename(path.with_suffix(".feather"))
    if restart:
        compactor.abort()
        compactor = _compactor(CatalogStore(split.root))
    with caplog.at_level(logging.WARNING):
        compactor.run_pass(_later())

    assert not [f for f in _codes(caplog, "book_gap") if f["kind"] == "silence"]
    assert _files(one.root, "data", "gaps") == _files(split.root, "data", "gaps")
    assert one.load_done().hours == split.load_done().hours


def test_a_dead_run_whose_trades_went_quiet_still_finishes_its_last_hour(tmp_path):
    feed = make_feed(START, END)
    store = _catalog(tmp_path, feed.messages)
    write_trades(store.live, "a-dead", make_trades(H0, H0 + 5 * MINUTE_NS), rotate_at=[])
    _compactor(store).run_pass(_later())
    assert sorted(store.load_done().hours[TRADES]) == [H0]


def test_the_row_cap_bounds_the_window_and_logs_once_per_pass(tmp_path, caplog):
    feed = make_feed(START, H0 + HOUR_NS + 20 * MINUTE_NS)
    store = _catalog(tmp_path, shuffle_within(feed.messages, 590 * SECOND_NS), rotate_at=[H0 + HOUR_NS])
    compactor = _compactor(store, max_rows=1_000)
    with caplog.at_level(logging.WARNING):
        compactor.run_pass(_later())
    capped = _codes(caplog, "catalog_window_capped")
    assert len(capped) == 1
    assert capped[0]["stream"] == DELTAS
    assert capped[0]["held_rows"] > 1_000
    for hour in store.load_done().hours[DELTAS]:
        ts = pq.read_table(str(store.data_file(DELTAS_TABLE, INSTRUMENT, hour))).column("ts_init").to_numpy()
        assert (ts[1:] >= ts[:-1]).all()


def test_a_pass_killed_after_finding_late_rows_reruns_to_the_same_files(tmp_path, monkeypatch):
    feed = make_feed(START, END)
    late = delay(feed.messages, message_at(feed.messages, H0 + 30 * MINUTE_NS), 700 * SECOND_NS)
    clean = _catalog(tmp_path / "clean", late)
    crashed_root = tmp_path / "crashed"
    shutil.copytree(clean.root, crashed_root)
    _compactor(clean).run_pass(_later())

    crashed = CatalogStore(crashed_root)
    calls = {"n": 0}

    class Killed(Exception):
        pass

    real_save = crashed.save_done

    def save(done):
        calls["n"] += 1
        if calls["n"] == 2:  # the first hour's commit: its quarantine is merged, done.json not yet
            raise Killed
        real_save(done)

    monkeypatch.setattr(crashed, "save_done", save)
    killed = _compactor(crashed)
    with pytest.raises(Killed):
        killed.run_pass(_later())
    killed.abort()
    monkeypatch.undo()
    assert (crashed_root / "quarantine").exists()

    _compactor(CatalogStore(crashed_root)).run_pass(_later())
    folders = ("data", "quarantine", "gaps", "manifests")
    assert _files(crashed_root, *folders) == _files(clean.root, *folders)
    a = json.loads(clean.done_path.read_text())
    b = json.loads(crashed.done_path.read_text())
    assert (a["hours"], a["incomplete"], a["carry"]) == (b["hours"], b["incomplete"], b["carry"])


@pytest.mark.parametrize("field", [{"compactor_reorder_window_s": -1}, {"compactor_reorder_max_rows": 0}])
def test_a_bad_window_setting_refuses_to_start(field):
    with pytest.raises(ValidationError):
        CompactorSettings(trading_env="demo", **field)


def test_the_window_settings_default_to_the_spec():
    settings = CompactorSettings(trading_env="demo")
    assert (settings.compactor_reorder_window_s, settings.compactor_reorder_max_rows) == (600, 2_000_000)
