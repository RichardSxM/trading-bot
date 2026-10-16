"""covers: spec 0009 AC-6, AC-7 (amended 2026-10-16: holes at a run's tail).

Run ``a-dead`` has its book from 06:00:07 to 06:59:50 and open interest that stops at 06:49:50, so
its tail (ten minutes, no rows) ends at the run's last book row, where the ``recorder_down`` to run
``b`` starts.
"""

import shutil
import time

import pytest
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, make_feed, make_open_interest, write_context, write_run

from recorder.compactor.days import read_gaps
from recorder.compactor.main import Compactor
from recorder.compactor.store import LIQUIDATION_TABLE, OPEN_INTEREST_TABLE, CatalogStore
from trading_core.marketdata.context_feed import Outage
from trading_core.marketdata.gaps import Gap

DAY = "2026-10-08"
A_END = H0 + HOUR_NS - 10 * SECOND_NS
OI_STOP = H0 + HOUR_NS - 610 * SECOND_NS
OPEN_AT = H0 + HOUR_NS - 300 * SECOND_NS  # the feed went down 5 minutes before the run's end
RAW = (OPEN_INTEREST_TABLE, LIQUIDATION_TABLE)


def _later():
    return time.time_ns() + 10 * HOUR_NS


def _compactor(store):
    return Compactor(store, grace_ns=0, floor_bytes=0)


def _run_a(store, *, oi_stop=OI_STOP):
    write_run(store.live, "a-dead", make_feed(H0 + 7 * SECOND_NS, A_END).messages, rotate_at=[])
    oi = make_open_interest(H0 + 3 * SECOND_NS, oi_stop)
    write_context(store.live, "a-dead", OPEN_INTEREST_TABLE, oi, rotate_at=[])
    return oi


def _run_b(store, run="b-dead"):
    b_start = H0 + HOUR_NS + 2 * SECOND_NS
    write_run(store.live, run, make_feed(b_start, H0 + 2 * HOUR_NS - SECOND_NS, seed=5).messages, rotate_at=[])


def _current(store, run="z-current", at=H0 + 5 * HOUR_NS):
    write_run(store.live, run, make_feed(at, at + 60 * SECOND_NS, seed=3).messages, rotate_at=[])


def _context_gaps(store):
    return [g for g in read_gaps(store, DAY, stream=None) if g.stream != "order_book_deltas"]


def _tails(store):
    return [g for g in _context_gaps(store) if g.kind in ("silence", "reconnect")]


def _a_end(store):
    (down,) = read_gaps(store, DAY)  # the book's own recorder_down starts at a's last book row
    assert down.kind == "recorder_down"
    return down.start_ns


def _store_with_open_line(tmp_path):
    store = CatalogStore(tmp_path)
    oi = _run_a(store)
    _run_b(store)
    _current(store)
    store.outages_file("a-dead").write_text(Outage(OPEN_AT, None, "idle").line())
    return store, oi


def test_a_quiet_tail_and_an_unclosed_outage_end_where_the_recorder_down_starts(tmp_path):
    store, oi = _store_with_open_line(tmp_path)

    _compactor(store).run_pass(_later())

    end = _a_end(store)
    last_oi = oi[-1].ts_init
    assert sorted(_tails(store)) == sorted(
        [
            Gap(last_oi, INSTRUMENT, "silence", OPEN_AT, stream=OPEN_INTEREST_TABLE),
            *(Gap(OPEN_AT, INSTRUMENT, "reconnect", end, stream=t) for t in RAW),
        ]
    )
    downs = [g for g in _context_gaps(store) if g.kind == "recorder_down" and g.stream == OPEN_INTEREST_TABLE]
    assert [g.start_ns for g in downs] == [end]  # touching, both kept
    done = store.load_done()
    assert (done.open_outages, done.pending_tails) == ({}, {})


def test_without_an_open_line_the_tail_silence_runs_to_the_run_end(tmp_path):
    store = CatalogStore(tmp_path)
    oi = _run_a(store)
    _run_b(store)
    _current(store)

    _compactor(store).run_pass(_later())

    assert _tails(store) == [Gap(oi[-1].ts_init, INSTRUMENT, "silence", _a_end(store), stream=OPEN_INTEREST_TABLE)]


def test_a_tail_under_the_silence_minimum_gives_nothing(tmp_path):
    store = CatalogStore(tmp_path)
    _run_a(store, oi_stop=A_END - 20 * SECOND_NS)
    _run_b(store)
    _current(store)

    _compactor(store).run_pass(_later())

    assert _tails(store) == []


def test_an_open_line_with_its_closed_line_gives_one_gap(tmp_path):
    store, _ = _store_with_open_line(tmp_path)
    closed_at = OPEN_AT + 60 * SECOND_NS
    with store.outages_file("a-dead").open("a") as f:
        f.write(Outage(OPEN_AT, closed_at, "idle").line())

    _compactor(store).run_pass(_later())

    reconnects = [g for g in _tails(store) if g.kind == "reconnect"]
    assert sorted(reconnects) == sorted(Gap(OPEN_AT, INSTRUMENT, "reconnect", closed_at, stream=t) for t in RAW)


def test_a_rerun_changes_nothing(tmp_path):
    store, _ = _store_with_open_line(tmp_path)
    _compactor(store).run_pass(_later())
    gaps_before = store.gap_file(DAY).read_bytes()
    done_before = store.done_path.read_text()

    _compactor(store).run_pass(_later())

    assert store.gap_file(DAY).read_bytes() == gaps_before
    assert store.done_path.read_text() == done_before


def test_a_pass_killed_before_the_tails_reruns_to_the_same_bytes(tmp_path, monkeypatch):
    clean, _ = _store_with_open_line(tmp_path / "clean")
    _compactor(clean).run_pass(_later())

    store, _ = _store_with_open_line(tmp_path / "killed")
    killed = _compactor(store)

    def boom():
        raise RuntimeError("killed")

    monkeypatch.setattr(killed, "_resolve_tails", boom)
    with pytest.raises(RuntimeError):
        killed.run_pass(_later())
    assert store.load_done().run_ends  # the run's end was saved with its recorder_down
    _compactor(store).run_pass(_later())

    assert store.gap_file(DAY).read_bytes() == clean.gap_file(DAY).read_bytes()
    assert store.done_path.read_text() == clean.done_path.read_text()


def test_tails_wait_for_the_next_run_and_survive_the_run_folder_going(tmp_path):
    """Pass 1: b is the current run, so a's end is not known yet and nothing is written; b's own open
    line waits too. Then a's folder goes, and b ends once c's book converts."""
    store = CatalogStore(tmp_path)
    oi = _run_a(store)
    _run_b(store, run="b-current")
    store.outages_file("a-dead").write_text(Outage(OPEN_AT, None, "idle").line())
    b_open = H0 + HOUR_NS + 1_800 * SECOND_NS
    store.outages_file("b-current").write_text(Outage(b_open, None, "closed").line())

    _compactor(store).run_pass(_later())

    assert _tails(store) == []
    done = store.load_done()
    assert set(done.open_outages) == {"a-dead", "b-current"}
    assert done.pending_tails == {f"{OPEN_INTEREST_TABLE}/{INSTRUMENT}": {"a-dead": oi[-1].ts_init}}

    shutil.rmtree(store.live / "a-dead", ignore_errors=True)
    _current(store, run="c-dead", at=H0 + 3 * HOUR_NS)  # its first book row gives b's end
    _current(store)
    _compactor(store).run_pass(_later())

    downs = sorted(g for g in read_gaps(store, DAY) if g.kind == "recorder_down")
    a_end, b_end = downs[0].start_ns, downs[1].start_ns
    assert sorted(_tails(store)) == sorted(
        [
            Gap(oi[-1].ts_init, INSTRUMENT, "silence", OPEN_AT, stream=OPEN_INTEREST_TABLE),
            *(Gap(OPEN_AT, INSTRUMENT, "reconnect", a_end, stream=t) for t in RAW),
            *(Gap(b_open, INSTRUMENT, "reconnect", b_end, stream=t) for t in RAW),
        ]
    )
    done = store.load_done()
    assert (done.open_outages, done.pending_tails) == ({}, {})


def test_a_run_with_no_book_rows_leaves_only_the_spanning_recorder_down(tmp_path):
    store = CatalogStore(tmp_path)
    write_run(store.live, "a-dead", make_feed(H0 + 7 * SECOND_NS, A_END).messages, rotate_at=[])
    m_oi = make_open_interest(H0 + HOUR_NS - 5 * SECOND_NS, H0 + HOUR_NS - SECOND_NS)
    write_context(store.live, "m-dead", OPEN_INTEREST_TABLE, m_oi, rotate_at=[])
    store.outages_file("m-dead").write_text(Outage(m_oi[-1].ts_init, None, "idle").line())
    _run_b(store)
    _current(store)

    _compactor(store).run_pass(_later())

    assert _tails(store) == []
    done = store.load_done()
    assert (done.open_outages, done.pending_tails) == ({}, {})


def test_a_tail_in_a_day_already_closed_rewrites_its_manifest(tmp_path):
    """A run ending at 23:50 after the day's manifest was written."""
    from recorder.compactor import days
    from trading_core.catalog.days import manifest_text
    from trading_core.marketdata.hours import day_of

    store = CatalogStore(tmp_path)
    compactor = _compactor(store)
    done = compactor.done
    day = day_of(H0)
    done.days.add(DAY)
    store.manifest_file(DAY).parent.mkdir(parents=True, exist_ok=True)
    store.manifest_file(DAY).write_text(manifest_text(days._manifest(store, done, day)))
    end = day + 23 * HOUR_NS + 50 * 60 * SECOND_NS
    done.streams[INSTRUMENT] = {"last_ts": end + HOUR_NS, "run": "b", "cleared": True}
    done.run_ends = {INSTRUMENT: {"a": end}}
    done.pending_tails = {f"{OPEN_INTEREST_TABLE}/{INSTRUMENT}": {"a": end - 600 * SECOND_NS}}
    before = store.manifest_file(DAY).read_text()

    assert compactor._resolve_tails() is True

    after = store.manifest_file(DAY).read_text()
    assert after != before
    assert manifest_text(days._manifest(store, done, day)) == after
    assert '"seconds": 600.0' in after
