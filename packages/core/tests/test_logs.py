"""covers: spec 0001 Logs (one JSON object per line on stdout, uvicorn routed through it)."""

import json
import logging
from datetime import datetime, timedelta

from trading_core.logs import configure_logging


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_each_record_is_one_json_line_with_the_required_fields(capsys):
    configure_logging(process="recorder", env="live")

    logging.getLogger("trading_core.x").info("hello %s", "world")

    (line,) = _lines(capsys)
    assert set(line) == {"ts", "level", "process", "env", "logger", "msg"}
    assert line["level"] == "INFO"
    assert line["process"] == "recorder"
    assert line["env"] == "live"
    assert line["logger"] == "trading_core.x"
    assert line["msg"] == "hello world"
    assert datetime.fromisoformat(line["ts"]).utcoffset() == timedelta(0)


def test_fields_are_merged_and_non_json_values_become_strings(capsys):
    configure_logging(process="trader", env="demo")

    logging.getLogger("t").warning("x", extra={"fields": {"offset_ms": 1.5, "path": object}})

    line = _lines(capsys)[0]
    assert line["offset_ms"] == 1.5
    assert isinstance(line["path"], str)


def test_exceptions_add_a_traceback_field(capsys):
    configure_logging(process="trader", env="demo")

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")

    line = _lines(capsys)[0]
    assert line["level"] == "ERROR"
    assert "ValueError: boom" in line["exc"]


def test_level_filters_records(capsys):
    configure_logging(process="trader", env="demo", level="WARNING")

    logging.getLogger("t").info("hidden")
    logging.getLogger("t").warning("shown")

    assert [line["msg"] for line in _lines(capsys)] == ["shown"]


def test_configuring_twice_does_not_duplicate_lines(capsys):
    configure_logging(process="trader", env="demo")
    configure_logging(process="trader", env="demo")

    logging.getLogger("t").info("once")

    assert len(_lines(capsys)) == 1


def test_uvicorn_loggers_go_through_the_json_handler(capsys):
    stray = logging.StreamHandler()
    logging.getLogger("uvicorn.error").addHandler(stray)

    configure_logging(process="dashboard", env="demo")
    logging.getLogger("uvicorn.access").info("GET /healthz 200")

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert _lines(capsys)[0]["logger"] == "uvicorn.access"
