"""covers: spec 0007 AC-15 (the sequence watch, the holes file, its stop path, file failure and exit 75)."""

import json
import logging
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from writer_fixture import fake_kernel

import recorder.main as recorder_main
from recorder.actor import RecordingActor
from recorder.disk import EXIT_DISK_FULL
from recorder.sequence_holes import HOLES_FILE, HolesFile, SequenceWatch
from recorder.settings import RecorderSettings

S = 1_000_000_000
T = 1_791_439_200 * S
CONFIRM = 600 * S


def _codes(caplog):
    return [(r.levelname, r.fields["code"]) for r in caplog.records if hasattr(r, "fields")]


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ts(n):
    return T + n * 1_000_000


def _watch(tmp_path, stops=None):
    path = tmp_path / "live" / "run-1" / HOLES_FILE
    stop = (lambda: stops.append(1)) if stops is not None else (lambda: pytest.fail("must not stop"))
    return SequenceWatch(lambda: path, confirm_ns=CONFIRM, stop=stop), path


def _observe(watch, numbers, now):
    for n in numbers:
        watch.observe(n, _ts(n), now)


def test_the_file_holds_the_start_each_tick_and_each_confirmed_hole(tmp_path, caplog):
    watch, path = _watch(tmp_path)
    with caplog.at_level(logging.INFO):
        watch.start(T)
        _observe(watch, [n for n in range(1, 1001) if not 400 <= n <= 410], T)
        watch.tick(T + 60 * S)
        watch.tick(T + CONFIRM)
    hole = {"first_missing": 400, "last_missing": 410, "missing": 11, "before_ns": _ts(399), "after_ns": _ts(411)}
    assert _lines(path) == [
        {"started_ns": T},
        {"checked_to_ns": T - 540 * S},  # the clock minus the confirm time
        hole,
        {"checked_to_ns": T},
    ]
    assert _codes(caplog) == [("ERROR", "book_sequence_hole")]
    assert caplog.records[0].fields == {"code": "book_sequence_hole", "run": "run-1", **hole}


def test_checked_to_never_passes_the_oldest_open_suspicion(tmp_path):
    watch, path = _watch(tmp_path)
    watch.start(T)
    _observe(watch, [1, 2], T)
    _observe(watch, [5], T + CONFIRM)
    watch.tick(T + CONFIRM + 60 * S)
    assert _lines(path)[-1] == {"checked_to_ns": _ts(2)}


def test_a_stop_with_an_open_suspicion_writes_it_as_a_hole_then_closed(tmp_path):
    watch, path = _watch(tmp_path)
    watch.start(T)
    _observe(watch, [1, 2, 5], T)
    watch.stop()
    assert _lines(path)[1:] == [
        {"first_missing": 3, "last_missing": 4, "missing": 2, "before_ns": _ts(2), "after_ns": _ts(5)},
        {"closed": True},
    ]


def test_numbers_after_their_hole_was_confirmed_log_one_warning_per_tick(tmp_path, caplog):
    watch, _ = _watch(tmp_path)
    watch.start(T)
    _observe(watch, [1, 2, 6], T)
    watch.tick(T + CONFIRM)
    with caplog.at_level(logging.INFO):
        _observe(watch, [3, 4, 5], T + CONFIRM + S)
        watch.tick(T + CONFIRM + 60 * S)
        watch.tick(T + CONFIRM + 120 * S)
    assert _codes(caplog) == [("ERROR", "book_sequence_hole"), ("WARNING", "book_sequence_late")]
    assert caplog.records[-1].fields == {"code": "book_sequence_late", "run": "run-1", "first_sequence": 3, "count": 3}


def test_a_failed_write_logs_critical_and_stops_once(tmp_path, caplog):
    stops: list[int] = []
    blocker = tmp_path / "live"
    blocker.write_text("not a folder")  # the run folder cannot be made
    watch, _ = _watch(tmp_path, stops)
    with caplog.at_level(logging.INFO):
        watch.start(T)
        watch.tick(T + 60 * S)
        watch.stop()
    assert watch.tripped
    assert stops == [1]
    assert _codes(caplog) == [("CRITICAL", "sequence_file_failed")]


def test_a_short_write_is_continued_so_no_line_is_torn(tmp_path, monkeypatch):
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data[:5]))  # the disk takes at most five bytes per call

    monkeypatch.setattr(os, "write", short_write)
    path = tmp_path / HOLES_FILE
    holes_file = HolesFile(path)
    holes_file.append([{"started_ns": T}])
    holes_file.append([{"checked_to_ns": T + 60 * S}, {"closed": True}])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"started_ns": T},
        {"checked_to_ns": T + 60 * S},
        {"closed": True},
    ]


def test_the_confirm_time_setting_is_validated():
    assert RecorderSettings(trading_env="demo").recorder_sequence_confirm_s == 600
    with pytest.raises(ValidationError):
        RecorderSettings(trading_env="demo", recorder_sequence_confirm_s=0)


class FakeNode:
    """Stands in for ``TradingNode``; ``run`` trips the sequence watch's file failure."""

    def __init__(self, config):
        self.actors: list[object] = []
        self.trader = SimpleNamespace(add_actor=self.actors.append, subscribe=lambda topic, handler: None)
        self.kernel = fake_kernel()

    def add_data_client_factory(self, *a):
        pass

    def build(self):
        pass

    def run(self):
        next(a for a in self.actors if isinstance(a, RecordingActor)).watch.tripped = True

    def dispose(self):
        pass


def test_main_exits_75_after_the_holes_file_failed(monkeypatch, tmp_path):
    settings = RecorderSettings(trading_env="demo", catalog_path=str(tmp_path))
    monkeypatch.setattr(recorder_main, "boot", lambda *a, **k: SimpleNamespace(settings=settings))
    monkeypatch.setattr(recorder_main, "wait_for_space", lambda path, floor: None)
    monkeypatch.setattr(recorder_main, "TradingNode", FakeNode)
    with pytest.raises(SystemExit) as info:
        recorder_main.main()
    assert info.value.code == EXIT_DISK_FULL
