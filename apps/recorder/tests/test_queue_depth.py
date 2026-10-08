"""covers: spec 0007 AC-13 (queue depth warnings with a fake probe and clock, the stop check, the wiring)."""

import logging

import pytest
from pydantic import ValidationError

from recorder.main import DATA_QUEUE_SIZE, RecorderSettings, build_config
from recorder.queue_depth import QueueWatch

CAP = 100_000
S = 1_000_000_000


def _codes(caplog):
    return [(r.levelname, r.fields["code"]) for r in caplog.records if hasattr(r, "fields")]


def _watch(depth):
    return QueueWatch(lambda: depth["now"], CAP, warn_pct=50)


def test_the_queue_watch_logs_each_level_at_most_once_per_minute(caplog):
    depth = {"now": 49_000}
    watch = _watch(depth)
    with caplog.at_level(logging.INFO):
        watch.tick(0)
        depth["now"] = 50_000
        watch.tick(1 * S)
        watch.tick(30 * S)  # throttled
        depth["now"] = CAP
        watch.tick(31 * S)
        watch.tick(60 * S)  # throttled
        depth["now"] = 50_000
        watch.tick(61 * S)  # 60 s after the first high
    assert _codes(caplog) == [
        ("WARNING", "recorder_queue_high"),
        ("ERROR", "recorder_queue_full"),
        ("WARNING", "recorder_queue_high"),
    ]
    assert caplog.records[0].fields == {"code": "recorder_queue_high", "depth": 50_000, "capacity": CAP}


def test_the_stop_check_reports_a_full_queue_as_an_error(caplog):
    with caplog.at_level(logging.INFO):
        _watch({"now": 99_000}).stop()
        _watch({"now": 10_000}).stop()
    assert _codes(caplog) == [("ERROR", "recorder_stop_queue_full"), ("INFO", "recorder_stop_queue")]


def test_the_node_config_keeps_the_watched_queue_capacity():
    config = build_config(RecorderSettings(trading_env="demo"))
    assert config.data_engine.qsize == DATA_QUEUE_SIZE == CAP


@pytest.mark.parametrize("pct", [0, 100])
def test_a_warn_level_outside_1_to_99_refuses_to_start(pct):
    with pytest.raises(ValidationError):
        RecorderSettings(trading_env="demo", recorder_queue_warn_pct=pct)
