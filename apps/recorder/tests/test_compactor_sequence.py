"""covers: spec 0007 AC-16 (holes file to `sequence` gaps, the ready gate, the book cut, the fail safe hole,
cleanup), AC-4 (precedence over reconnect and silence), AC-5 (incomplete hours), AC-11 (reruns)."""

import json
import logging
import os
import shutil
import time

import pyarrow as pa
import pytest
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, _table, make_feed, write_run
from nautilus_trader.core import nautilus_pyo3 as p3
from test_compactor_lifecycle import _snapshot

from recorder.compactor import convert
from recorder.compactor.convert import BookCarry
from recorder.compactor.days import read_gaps
from recorder.compactor.main import Compactor
from recorder.compactor.store import DELTAS_TABLE, CatalogStore, stream_key
from trading_core.marketdata.gaps import BookScan, Gap
from trading_core.marketdata.hours import DAY_NS, day_of

S = SECOND_NS
MINUTE_NS = 60 * S
ETH = "ETHUSDT-PERP.BINANCE"
BTC_DELTAS = stream_key(DELTAS_TABLE, INSTRUMENT)
ETH_DELTAS = stream_key(DELTAS_TABLE, ETH)
DAY = "2026-10-08"


def _later():
    return max(time.time_ns(), day_of(H0) + 30 * DAY_NS)


def _compactor(store, **kwargs):
    return Compactor(store, grace_ns=0, floor_bytes=0, **kwargs)


def _hole(before, after, missing=7):
    return {
        "first_missing": 100,
        "last_missing": 100 + missing - 1,
        "missing": missing,
        "before_ns": before,
        "after_ns": after,
    }


def _write_holes(store, run, *lines, tail=""):
    path = store.holes_file(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + tail)


def _append_holes(store, run, *lines):
    with store.holes_file(run).open("a") as file:
        file.write("".join(json.dumps(line) + "\n" for line in lines))


def _current_run(store, start=H0 + 6 * HOUR_NS):
    write_run(store.live, "z-current", make_feed(start, start + MINUTE_NS, seed=3).messages, rotate_at=[])


def _first_last(messages):
    return messages[0][0].ts_init, messages[-1][0].ts_init


def _committed(root):
    """The catalog files and done.json, without the open hour's temp file (it is never read)."""
    files, done = _snapshot(root)
    return {k: v for k, v in files.items() if not k.endswith(".tmp")}, done


def _codes(caplog, code):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == code]


BEFORE = H0 + HOUR_NS - 10 * S  # 06:59:50, so the gap crosses into the next hour


def _two_instruments_with_a_hole(store, run="a-dead"):
    """BTC and ETH in one run, a hole at 06:59:50, and each feed's next CLEAR 30 s and 35 s
    after 07:00. Returns the CLEAR times."""
    btc_clear, eth_clear = H0 + HOUR_NS + 30 * S, H0 + HOUR_NS + 35 * S
    end = H0 + HOUR_NS + 20 * MINUTE_NS
    btc = make_feed(H0 + 7 * S, btc_clear).messages + make_feed(btc_clear, end, seed=9).messages
    eth = (
        make_feed(H0 + 7 * S, eth_clear, seed=21, instrument=ETH).messages
        + make_feed(eth_clear, end, seed=23, instrument=ETH).messages
    )
    write_run(store.live, run, btc, rotate_at=[H0 + HOUR_NS])
    write_run(store.live, run, eth, rotate_at=[H0 + HOUR_NS], instrument=ETH)
    _write_holes(
        store,
        run,
        {"started_ns": H0},
        _hole(BEFORE, BEFORE + S),
        {"checked_to_ns": H0 + 2 * HOUR_NS},
        {"closed": True},
    )
    return btc_clear, eth_clear


def test_a_hole_is_a_sequence_gap_for_every_instrument_of_the_run(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    btc_clear, eth_clear = _two_instruments_with_a_hole(store)
    _current_run(store)
    with caplog.at_level(logging.INFO):
        _compactor(store).run_pass(_later())

    gaps = read_gaps(store, DAY)
    # Each gap ends at that instrument's own CLEAR, which is not also a reconnect; no silence inside.
    assert [g for g in gaps if g.kind != "recorder_down"] == [
        Gap(BEFORE, INSTRUMENT, "sequence", btc_clear),
        Gap(BEFORE, ETH, "sequence", eth_clear),
    ]
    logged = [f for f in _codes(caplog, "book_gap") if f["kind"] == "sequence"]
    assert [f["missing"] for f in logged] == [7, 7]
    done = store.load_done()
    for key in (BTC_DELTAS, ETH_DELTAS):
        # Both hours the gap touches are incomplete, and the second starts without a snapshot.
        assert done.incomplete[key] == {H0: 0, H0 + HOUR_NS: 0}
        assert done.hours[key][H0 + HOUR_NS]["snapshot_rows"] == 0
    # The CLEAR made the book valid again, so it is carried at the run's end.
    assert done.carry[INSTRUMENT] == H0 + HOUR_NS
    assert _codes(caplog, "sequence_unchecked_run") == [{"code": "sequence_unchecked_run", "run": "z-current"}]


def test_an_hour_waits_for_the_holes_file_and_a_rerun_matches_one_pass(tmp_path):
    """The hole is confirmed after the first pass read the file; the hour waits, and the result
    equals a catalog that read the whole file at once."""
    stepwise, at_once = tmp_path / "stepwise", tmp_path / "at_once"
    store = CatalogStore(stepwise)
    feed = make_feed(H0 + 7 * S, H0 + HOUR_NS + 10 * MINUTE_NS).messages
    write_run(store.live, "a-live", feed, rotate_at=[H0 + HOUR_NS])
    _write_holes(store, "a-live", {"started_ns": H0}, {"checked_to_ns": H0 + 50 * MINUTE_NS})
    shutil.copytree(stepwise, at_once)
    late_lines = (_hole(H0 + 55 * MINUTE_NS, H0 + 55 * MINUTE_NS + S), {"checked_to_ns": H0 + HOUR_NS + 5 * MINUTE_NS})

    compactor = _compactor(store, window_ns=0)
    compactor.run_pass(_later())
    assert H0 not in store.load_done().hours.get(BTC_DELTAS, {})
    _append_holes(store, "a-live", *late_lines)
    compactor.run_pass(_later())

    other = CatalogStore(at_once)
    _append_holes(other, "a-live", *late_lines)
    _compactor(other, window_ns=0).run_pass(_later())

    done = store.load_done()
    assert H0 in done.hours[BTC_DELTAS]
    assert done.incomplete[BTC_DELTAS] == {H0: 0}  # the gap is still open: the run goes on
    assert _committed(stepwise) == _committed(at_once)


def test_a_run_that_ended_without_closed_gets_the_fail_safe_hole(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    btc = make_feed(H0 + 7 * S, H0 + 20 * MINUTE_NS).messages
    eth = make_feed(H0 + 7 * S, H0 + 5 * MINUTE_NS, seed=21, instrument=ETH).messages
    write_run(store.live, "a-dead", btc, rotate_at=[])
    write_run(store.live, "a-dead", eth, rotate_at=[], instrument=ETH)
    _write_holes(store, "a-dead", {"started_ns": H0}, {"checked_to_ns": H0 + 10 * MINUTE_NS})
    _current_run(store)
    with caplog.at_level(logging.WARNING):
        _compactor(store).run_pass(_later())

    sequence = [g for g in read_gaps(store, DAY) if g.kind == "sequence"]
    # ETH's last row is before the unchecked tail, so only BTC is marked.
    assert sequence == [Gap(H0 + 10 * MINUTE_NS, INSTRUMENT, "sequence", _first_last(btc)[1])]
    assert [f["missing"] for f in _codes(caplog, "book_gap") if f["kind"] == "sequence"] == [-1]


def test_a_holes_file_with_only_a_torn_line_makes_the_whole_run_one_gap(tmp_path):
    store = CatalogStore(tmp_path)
    btc = make_feed(H0 + 7 * S, H0 + 20 * MINUTE_NS).messages
    write_run(store.live, "a-dead", btc, rotate_at=[])
    _write_holes(store, "a-dead", tail='{"started_ns": 17914')
    _current_run(store)
    _compactor(store).run_pass(_later())

    first, last = _first_last(btc)
    assert [g for g in read_gaps(store, DAY) if g.kind == "sequence"] == [Gap(first, INSTRUMENT, "sequence", last)]
    assert store.load_done().incomplete[BTC_DELTAS] == {H0: 0}


def test_a_run_without_a_holes_file_converts_as_before_and_logs_once(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    write_run(store.live, "a-dead", make_feed(H0 + 7 * S, H0 + 20 * MINUTE_NS).messages, rotate_at=[])
    _current_run(store)
    with caplog.at_level(logging.INFO):
        _compactor(store).run_pass(_later())
        _compactor(store).run_pass(_later())

    assert [f["run"] for f in _codes(caplog, "sequence_unchecked_run")] == ["a-dead", "z-current"]
    assert not [g for g in read_gaps(store, DAY) if g.kind == "sequence"]
    assert BTC_DELTAS not in store.load_done().incomplete


def test_a_pass_killed_inside_a_sequence_gap_reruns_to_the_same_files(tmp_path, monkeypatch):
    clean, crashed = tmp_path / "clean", tmp_path / "crashed"
    store = CatalogStore(clean)
    _two_instruments_with_a_hole(store)
    _current_run(store)
    shutil.copytree(clean, crashed)
    _compactor(store).run_pass(_later())

    class Killed(Exception):
        pass

    calls = {"n": 0}
    real_finish = convert.HourStream._finish

    def finish_then_die(self, done):
        real_finish(self, done)
        calls["n"] += 1
        if calls["n"] == 1:  # BTC's 06:00 hour is committed with the gap still open
            raise Killed

    monkeypatch.setattr(convert.HourStream, "_finish", finish_then_die)
    killed = _compactor(CatalogStore(crashed))
    with pytest.raises(Killed):
        killed.run_pass(_later())
    killed.abort()
    monkeypatch.undo()
    _compactor(CatalogStore(crashed)).run_pass(_later())

    assert _snapshot(crashed) == _snapshot(clean)


def test_a_run_folder_with_only_a_holes_file_is_removed_once_a_newer_run_writes(tmp_path):
    store = CatalogStore(tmp_path)
    _current_run(store)
    _write_holes(store, "0-died-at-start", {"started_ns": H0})
    os.utime(store.holes_file("0-died-at-start"), ns=(H0, H0))
    _write_holes(store, "zz-just-started", {"started_ns": time.time_ns()})
    _compactor(store).run_pass(_later())

    assert not (store.live / "0-died-at-start").exists()
    assert store.holes_file("zz-just-started").exists()


def _rows(messages):
    return [_table(p3.book_deltas_to_arrow_record_batch_bytes(m)) for m in messages]


def _book(carry):
    return [(str(level.first().price), str(level.first().size)) for level in [*carry.book.bids(), *carry.book.asks()]]


def test_the_book_is_cut_at_a_hole_and_starts_again_only_at_the_clear_that_ends_it():
    first = make_feed(H0, H0 + MINUTE_NS).messages
    second = make_feed(H0 + MINUTE_NS, H0 + 2 * MINUTE_NS, seed=9).messages  # starts with a CLEAR
    table = pa.concat_tables(_rows(first + second)).combine_chunks()
    clear = sum(len(m) for m in first)
    cut = clear // 2

    carry = BookCarry(INSTRUMENT)
    carry.apply(table.slice(0, clear), BookScan([], cuts=[cut]))
    assert not carry.valid
    # Still inside the gap: the CLEAR in this chunk is not the one that ends it.
    carry.apply(table.slice(clear), BookScan([], blocked=True))
    assert not carry.valid

    reference = BookCarry(INSTRUMENT)
    reference.apply(table.slice(clear))
    resumed = BookCarry(INSTRUMENT)
    resumed.apply(table, BookScan([], cuts=[cut], resumes=[clear]))
    assert resumed.valid
    assert _book(resumed) == _book(reference)
