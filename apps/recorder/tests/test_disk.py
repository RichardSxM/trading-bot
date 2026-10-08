"""covers: spec 0007 AC-7 (the hard floor guard, the wait at start, exit code 75, the health check)."""

import logging
import os
import signal
import threading
from types import SimpleNamespace

import pytest

import recorder.main as recorder_main
from recorder import disk, health
from recorder.actor import DiskGuardActor
from recorder.compactor.store import DELTAS_TABLE, INSTRUMENTS_TABLE, TRADES_TABLE
from recorder.disk import EXIT_DISK_FULL, DiskGuard, wait_for_space
from recorder.live_files import DELTAS_STREAM, INSTRUMENTS_STREAM, TRADES_STREAM
from recorder.settings import RecorderSettings

GB = 1_000_000_000
FLOOR = 2 * GB


def _codes(caplog):
    return [(r.levelname, r.fields["code"]) for r in caplog.records if hasattr(r, "fields")]


def test_the_guard_logs_and_stops_once_below_the_floor(tmp_path, caplog):
    free = {"now": 3 * GB}
    stops = []
    guard = DiskGuard(tmp_path, FLOOR, free=lambda _: free["now"], stop=lambda: stops.append(1))
    with caplog.at_level(logging.INFO):
        guard.check()
        assert stops == []
        free["now"] = FLOOR - 1
        guard.check()
        guard.check()  # already stopping: no second log or stop
    assert guard.tripped
    assert stops == [1]
    assert _codes(caplog) == [("CRITICAL", "catalog_disk_full")]
    assert caplog.records[0].fields == {"code": "catalog_disk_full", "free_bytes": FLOOR - 1, "floor_bytes": FLOOR}


def test_exactly_the_floor_is_enough(tmp_path):
    guard = DiskGuard(tmp_path, FLOOR, free=lambda _: FLOOR, stop=lambda: pytest.fail("must not stop"))
    guard.check()
    assert not guard.tripped


def test_stopping_sends_sigterm_and_arms_the_exit_75_fallback(monkeypatch):
    kills = []
    timers = []

    class FakeTimer:
        def __init__(self, interval, function, args):
            timers.append((interval, function, args))
            self.daemon = False

        def start(self):
            assert self.daemon

    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(threading, "Timer", FakeTimer)

    disk.stop_process()

    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert timers == [(disk.STOP_TIMEOUT_S, os._exit, (EXIT_DISK_FULL,))]


def test_the_wait_at_start_logs_once_and_then_ok(tmp_path, caplog):
    readings = iter([GB, GB, GB, 5 * GB])
    sleeps: list[float] = []
    with caplog.at_level(logging.INFO):
        wait_for_space(tmp_path, FLOOR, free=lambda _: next(readings), sleep=sleeps.append)
    assert sleeps == [30.0, 30.0, 30.0]
    assert _codes(caplog) == [("CRITICAL", "catalog_disk_full"), ("INFO", "catalog_disk_ok")]


def test_no_wait_and_no_log_with_enough_space(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        wait_for_space(tmp_path, FLOOR, free=lambda _: FLOOR, sleep=lambda _: pytest.fail("must not wait"))
    assert _codes(caplog) == []


class FakeNode:
    """Stands in for ``TradingNode``: collects the actors, and ``run`` trips the disk guard."""

    def __init__(self, config):
        self.actors: list[object] = []
        self.trader = SimpleNamespace(add_actor=self.actors.append)
        self.disposed = False

    def add_data_client_factory(self, *a):
        pass

    def build(self):
        pass

    def run(self):
        guard = next(a for a in self.actors if isinstance(a, DiskGuardActor)).guard
        guard.tripped = True

    def dispose(self):
        self.disposed = True


def _boot(monkeypatch, tmp_path):
    settings = RecorderSettings(trading_env="demo", catalog_path=str(tmp_path))
    monkeypatch.setattr(recorder_main, "boot", lambda *a, **k: SimpleNamespace(settings=settings))


def test_main_waits_before_building_the_node_and_exits_75_after_a_trip(monkeypatch, tmp_path):
    _boot(monkeypatch, tmp_path)
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(recorder_main, "wait_for_space", lambda path, floor: events.append(("wait", path, floor)))
    nodes = []

    def node(config):
        events.append(("node",))
        nodes.append(FakeNode(config))
        return nodes[-1]

    monkeypatch.setattr(recorder_main, "TradingNode", node)

    with pytest.raises(SystemExit) as info:
        recorder_main.main()

    assert info.value.code == EXIT_DISK_FULL
    assert events == [("wait", tmp_path, FLOOR), ("node",)]
    assert nodes[0].disposed


def test_the_live_stream_names_match_the_catalog_tables():
    assert (DELTAS_STREAM, TRADES_STREAM, INSTRUMENTS_STREAM) == (DELTAS_TABLE, TRADES_TABLE, INSTRUMENTS_TABLE)


NOW = 10_000.0


def _feather(tmp_path, run, created_ns, mtime, table=DELTAS_STREAM, instrument="PF_XBTUSD.KRAKEN"):
    path = tmp_path / "live" / run / table / instrument / f"{table}_{created_ns}.feather"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def beat(tmp_path):
    path = tmp_path / "alive"
    path.touch()
    os.utime(path, (NOW - 5, NOW - 5))
    return path


def _check(tmp_path, beat, free=5 * GB):
    return health.check(tmp_path, beat, FLOOR, now=NOW, free=lambda _: free)


def test_healthy_when_all_three_hold(tmp_path, beat):
    _feather(tmp_path, "run-b", 2_000, NOW - 59)
    assert _check(tmp_path, beat) is None


def test_unhealthy_on_an_old_heartbeat(tmp_path, beat):
    _feather(tmp_path, "run-b", 2_000, NOW - 1)
    os.utime(beat, (NOW - 30, NOW - 30))
    assert "heartbeat" in _check(tmp_path, beat)


def test_unhealthy_on_a_stale_delta_file(tmp_path, beat):
    _feather(tmp_path, "run-b", 2_000, NOW - 60)
    _feather(tmp_path, "run-b", 2_500, NOW - 1, table=TRADES_STREAM)  # trades alone do not count
    assert "order_book_deltas" in _check(tmp_path, beat)


def test_only_the_current_run_counts(tmp_path, beat):
    # A dead run's file written just now (impossible in practice) must not hide a stale current run.
    _feather(tmp_path, "run-a", 1_000, NOW - 1)
    _feather(tmp_path, "run-b", 2_000, NOW - 120)
    assert "order_book_deltas" in _check(tmp_path, beat)


def test_unhealthy_before_the_first_file(tmp_path, beat):
    assert "order_book_deltas" in _check(tmp_path, beat)


def test_unhealthy_below_the_hard_floor(tmp_path, beat):
    _feather(tmp_path, "run-b", 2_000, NOW - 1)
    assert "hard floor" in _check(tmp_path, beat, free=FLOOR - 1)
