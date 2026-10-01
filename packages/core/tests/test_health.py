"""covers: spec 0001 Compose health (node healthy while the heartbeat file is under 30 s old)."""

import os
import sys

import pytest

from trading_core import health
from trading_core.health import is_fresh


@pytest.fixture
def beat(tmp_path):
    path = tmp_path / "alive"
    path.touch()
    os.utime(path, (1_000.0, 1_000.0))
    return path


@pytest.mark.parametrize(("now", "fresh"), [(1_000.0, True), (1_029.9, True), (1_030.0, False), (1_500.0, False)])
def test_is_fresh_while_younger_than_max_age(beat, now, fresh):
    assert is_fresh(beat, 30.0, now=now) is fresh


def test_missing_file_is_not_fresh(tmp_path):
    assert is_fresh(tmp_path / "never-written", 30.0) is False


def _run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["health", *args])
    with pytest.raises(SystemExit) as exit_info:
        health.main()
    return exit_info.value.code


def test_main_exits_0_for_a_just_touched_file(monkeypatch, tmp_path):
    path = tmp_path / "alive"
    path.touch()

    assert _run_main(monkeypatch, str(path), "30") == 0


def test_main_exits_1_for_a_stale_file(monkeypatch, beat):
    assert _run_main(monkeypatch, str(beat), "30") == 1


def test_main_exits_1_when_the_file_is_missing(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, str(tmp_path / "missing")) == 1


def test_main_defaults_match_the_compose_healthcheck():
    assert health.DEFAULT_PATH == "/run/heartbeat/alive"
    assert health.DEFAULT_MAX_AGE_S == 30.0
