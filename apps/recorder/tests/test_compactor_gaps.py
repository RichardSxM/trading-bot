"""covers: spec 0007 AC-4 (gap kinds, gap files, `book_gap`), AC-5 (day close and manifest), AC-11 (gap rows
after a crash and rerun), AC-3 (the hour after a restart starts from the feed snapshot)."""

import json
import logging
import shutil
import time

import pytest
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, make_feed, write_run
from test_compactor_lifecycle import _snapshot

from recorder.compactor.days import read_gaps
from recorder.compactor.main import Compactor
from recorder.compactor.store import DELTAS_TABLE, CatalogStore, stream_key
from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.hours import DAY_NS, day_of

MINUTE_NS = 60 * SECOND_NS
DELTAS = stream_key(DELTAS_TABLE, INSTRUMENT)
DAY = day_of(H0)  # 2026-10-08 00:00 UTC


def _later():
    # Past every fixture hour (the midnight runs end on 2026-10-10), so each dead run's last hour is ready.
    return max(time.time_ns(), DAY + 30 * DAY_NS)


def _compactor(store, **kwargs):
    return Compactor(store, grace_ns=0, floor_bytes=0, **kwargs)


def _first_last(messages):
    return messages[0][0].ts_init, messages[-1][0].ts_init


def _current_run(store, start):
    later = make_feed(start, start + MINUTE_NS, seed=3)
    write_run(store.live, "z-current", later.messages, rotate_at=[])


def _restart(store):
    """Run a, a 40 s hole, then run b whose feed reconnects (a new CLEAR) 5 s after a pause."""
    a = make_feed(H0 + 7 * SECOND_NS, H0 + 30 * MINUTE_NS).messages
    b_start = H0 + 30 * MINUTE_NS + 40 * SECOND_NS
    b1 = make_feed(b_start, H0 + 50 * MINUTE_NS, seed=9).messages
    b2 = make_feed(H0 + 50 * MINUTE_NS + 5 * SECOND_NS, H0 + HOUR_NS + 30 * MINUTE_NS, seed=13).messages
    write_run(store.live, "a-dead", a, rotate_at=[])
    write_run(store.live, "b-dead", b1 + b2, rotate_at=[H0 + HOUR_NS])
    _current_run(store, H0 + 6 * HOUR_NS)
    return a, b1, b2


def test_a_restart_and_a_reconnect_each_give_one_gap(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    a, b1, b2 = _restart(store)
    with caplog.at_level(logging.WARNING):
        _compactor(store).run_pass(_later())

    expected = [
        Gap(_first_last(a)[1], INSTRUMENT, "recorder_down", _first_last(b1)[0]),
        Gap(_first_last(b1)[1], INSTRUMENT, "reconnect", _first_last(b2)[0]),
    ]
    assert read_gaps(store, "2026-10-08") == expected
    logged = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == "book_gap"]
    assert [(f["kind"], f["start_ns"], f["end_ns"]) for f in logged] == [
        (g.kind, g.start_ns, g.end_ns) for g in expected
    ]
    assert logged[0]["duration_s"] == pytest.approx(expected[0].duration_s, abs=1e-3)
    # The reconnect's CLEAR made the book valid again, so the next hour starts from a snapshot.
    assert store.load_done().hours[DELTAS][H0 + HOUR_NS]["snapshot_rows"] > 0


def _with_hole(messages, start, length):
    return [m for m in messages if not start < m[0].ts_init < start + length]


@pytest.mark.parametrize(("hole_s", "silences"), [(12, 1), (8, 0)])
def test_silence_counts_from_ten_seconds(tmp_path, hole_s, silences):
    store = CatalogStore(tmp_path)
    feed = make_feed(H0 + 7 * SECOND_NS, H0 + 20 * MINUTE_NS).messages
    hole = H0 + 10 * MINUTE_NS
    write_run(store.live, "a-dead", _with_hole(feed, hole, hole_s * SECOND_NS), rotate_at=[])
    _current_run(store, H0 + 6 * HOUR_NS)
    _compactor(store).run_pass(_later())

    gaps = read_gaps(store, "2026-10-08")
    assert [g.kind for g in gaps] == ["silence"] * silences
    for gap in gaps:
        assert gap.start_ns <= hole < hole + hole_s * SECOND_NS <= gap.end_ns


def _midnight(store):
    """Run a ends 23:50, run b starts 00:20 the next day and ends 09:30, run c starts 12:10.

    Feeds are sparse (a message every 20 to 30 s) to stay small, so the silence threshold is
    raised for these tests.
    """
    sparse = (20_000, 30_000)
    a = make_feed(DAY + 23 * HOUR_NS + 5 * SECOND_NS, DAY + 23 * HOUR_NS + 50 * MINUTE_NS, step_ms=sparse).messages
    b = make_feed(DAY + DAY_NS + 20 * MINUTE_NS, DAY + DAY_NS + 9 * HOUR_NS + 30 * MINUTE_NS, seed=9, step_ms=sparse)
    c = make_feed(DAY + DAY_NS + 12 * HOUR_NS + 10 * MINUTE_NS, DAY + 2 * DAY_NS + HOUR_NS, seed=13, step_ms=sparse)
    write_run(store.live, "a-dead", a, rotate_at=[])
    write_run(store.live, "b-dead", b.messages, rotate_at=[])
    write_run(store.live, "c-dead", c.messages, rotate_at=[])
    _current_run(store, DAY + 2 * DAY_NS + 5 * HOUR_NS)
    return a, b.messages, c.messages


def test_a_gap_across_midnight_lies_in_its_start_day_and_each_manifest_counts_its_part(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    a, b, c = _midnight(store)
    with caplog.at_level(logging.INFO):
        _compactor(store, silence_ns=HOUR_NS).run_pass(_later())

    first_down = Gap(_first_last(a)[1], INSTRUMENT, "recorder_down", _first_last(b)[0])
    second_down = Gap(_first_last(b)[1], INSTRUMENT, "recorder_down", _first_last(c)[0])
    assert read_gaps(store, "2026-10-08") == [first_down]
    assert read_gaps(store, "2026-10-09") == [second_down]

    # Day one starts at its first recorded hour (23:00); day two has 10:00 and 11:00 empty inside
    # the second gap and still closes; day three is not finished.
    done = store.load_done()
    assert done.days == {"2026-10-08", "2026-10-09"}
    assert done.empty[INSTRUMENT] == {DAY + DAY_NS + 10 * HOUR_NS, DAY + DAY_NS + 11 * HOUR_NS}
    assert not store.manifest_file("2026-10-10").exists()

    one = json.loads(store.manifest_file("2026-10-08").read_text())
    two = json.loads(store.manifest_file("2026-10-09").read_text())
    midnight = DAY + DAY_NS
    [gap_one] = one["gaps"]
    assert (gap_one["instrument_id"], gap_one["kind"]) == (INSTRUMENT, "recorder_down")
    assert gap_one["seconds"] == pytest.approx((midnight - first_down.start_ns) / SECOND_NS)
    [gap_two] = two["gaps"]
    two_ns = first_down.end_ns - midnight + second_down.end_ns - second_down.start_ns
    assert gap_two["seconds"] == pytest.approx(two_ns / SECOND_NS)

    hours = done.hours[DELTAS]
    day_two = [stats for hour, stats in hours.items() if midnight <= hour < midnight + DAY_NS]
    assert two["streams"] == [
        {
            "instrument_id": INSTRUMENT,
            "type": DELTAS_TABLE,
            "rows": sum(s["rows"] for s in day_two),
            "bytes": sum(s["bytes"] for s in day_two),
            "hours": 22,
            "late_rows": 0,
            "incomplete_hours": [],
        }
    ]
    assert two["total_bytes"] == two["streams"][0]["bytes"]
    closed = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == "catalog_day_closed"]
    assert [f["date"] for f in closed] == ["2026-10-08", "2026-10-09"]
    assert closed[1]["total_bytes"] == two["total_bytes"]


@pytest.mark.parametrize("at", [0.25, 0.5, 0.75])
def test_a_pass_killed_after_a_commit_reruns_to_the_same_gaps_and_manifests(tmp_path, monkeypatch, at):
    clean, crashed = tmp_path / "clean", tmp_path / "crashed"
    store = CatalogStore(clean)
    _midnight(store)
    shutil.copytree(clean, crashed)
    commits = {"n": 0}
    real_clean_save = store.save_done

    def count(done):
        real_clean_save(done)
        commits["n"] += 1

    monkeypatch.setattr(store, "save_done", count)
    _compactor(store, silence_ns=HOUR_NS).run_pass(_later())
    monkeypatch.undo()

    crashed_store = CatalogStore(crashed)
    calls = {"n": 0}
    kill_at = int(commits["n"] * at)

    class Killed(Exception):
        pass

    real_save = crashed_store.save_done

    def save_then_die(done):
        real_save(done)
        calls["n"] += 1
        if calls["n"] == kill_at:
            raise Killed

    monkeypatch.setattr(crashed_store, "save_done", save_then_die)
    killed = _compactor(crashed_store, silence_ns=HOUR_NS)
    with pytest.raises(Killed):
        killed.run_pass(_later())
    killed.abort()
    monkeypatch.undo()

    _compactor(CatalogStore(crashed), silence_ns=HOUR_NS).run_pass(_later())
    assert not list(crashed.rglob("*.tmp"))
    assert _snapshot(crashed) == _snapshot(clean)
