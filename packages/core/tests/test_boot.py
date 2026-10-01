"""covers: spec 0001 boot order (settings, JSON logs, env guard, then clock check) and its refusals."""

import json

import pytest

from trading_core import clock
from trading_core.boot import boot
from trading_core.clock import ClockResult
from trading_core.env_guard import EnvGuardRefused
from trading_core.settings import Settings


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PROCESS_NAME", "trader")
    monkeypatch.setenv("CLOCK_SERVERS", "a, b")
    return monkeypatch


@pytest.fixture
def clock_calls(monkeypatch):
    """Replace the network query; tests set ``clock_calls.result`` to steer the outcome."""

    class Calls(list[list[str]]):
        result = ClockResult(offset_ms=12.0, answered=2, queried=2)

    calls = Calls()

    def fake_measure(servers):
        calls.append(list(servers))
        return calls.result

    monkeypatch.setattr(clock, "measure", fake_measure)
    return calls


def test_happy_path_returns_settings_and_logs_starting_then_clock_passed(env, clock_calls, capsys):
    settings = boot("node")

    assert isinstance(settings, Settings)
    assert settings.trading_env == "demo"
    assert clock_calls == [["a", "b"]]
    lines = _lines(capsys)
    assert [line["msg"] for line in lines] == ["starting", "clock check passed"]
    assert lines[0] | {"ts": None} == {
        "ts": None,
        "level": "INFO",
        "process": "trader",
        "env": "demo",
        "logger": "trading_core.boot",
        "msg": "starting",
        "kind": "node",
    }


def test_boot_uses_the_given_settings_class(env, clock_calls):
    class AppSettings(Settings):
        extra_field: str = "default"

    assert isinstance(boot("node", settings_cls=AppSettings), AppSettings)


@pytest.mark.parametrize("value", [None, "paper", ""])
def test_missing_or_unknown_trading_env_exits_1_with_one_critical_line(env, clock_calls, capsys, value):
    if value is None:
        env.delenv("TRADING_ENV")
    else:
        env.setenv("TRADING_ENV", value)

    with pytest.raises(SystemExit) as exit_info:
        boot("node")

    assert exit_info.value.code == 1
    assert clock_calls == []
    (line,) = _lines(capsys)
    assert line["level"] == "CRITICAL"
    assert line["msg"] == "invalid configuration, refusing to start"
    assert line["process"] == "trader"
    assert any(error["loc"] == ["trading_env"] for error in line["errors"])


def test_invalid_config_log_never_echoes_the_bad_value(env, clock_calls, capsys):
    env.setenv("POSTGRES_PORT", "s3cr3t-not-a-port")

    with pytest.raises(SystemExit):
        boot("node")

    assert "s3cr3t-not-a-port" not in capsys.readouterr().out


def test_guard_refusal_exits_1_before_the_clock_check(env, clock_calls, capsys):
    seen = []

    def guard(settings):
        seen.append(settings.trading_env)
        raise EnvGuardRefused("trader in demo must not see Kraken keys")

    with pytest.raises(SystemExit) as exit_info:
        boot("node", guard=guard)

    assert exit_info.value.code == 1
    assert seen == ["demo"]
    assert clock_calls == []  # covers: guard runs before the clock check
    lines = _lines(capsys)
    assert [line["msg"] for line in lines] == ["starting", "env guard refused, refusing to start"]
    assert lines[-1]["level"] == "ERROR"
    assert lines[-1]["code"] == "env_guard_refused"
    assert lines[-1]["reason"] == "trader in demo must not see Kraken keys"


def test_guard_that_passes_lets_boot_continue_to_the_clock_check(env, clock_calls):
    boot("node", guard=lambda settings: None)

    assert len(clock_calls) == 1


def test_errors_other_than_a_refusal_are_not_swallowed_by_boot(env, clock_calls):
    def guard(settings):
        raise KeyError("bug in a guard")

    with pytest.raises(KeyError):
        boot("node", guard=guard)


def test_clock_refusal_exits_1_with_a_critical_line(env, clock_calls, capsys):
    env.setenv("TRADING_ENV", "live")
    clock_calls.result = ClockResult(offset_ms=None, answered=0, queried=2)

    with pytest.raises(SystemExit) as exit_info:
        boot("node")

    assert exit_info.value.code == 1
    line = _lines(capsys)[-1]
    assert line["level"] == "CRITICAL"
    assert line["msg"] == "no clock server answered; refusing to start in live"
    assert line["answered"] == 0


def test_dashboard_boots_even_when_the_clock_is_far_off(env, clock_calls):
    env.setenv("TRADING_ENV", "live")
    clock_calls.result = ClockResult(offset_ms=10_000.0, answered=2, queried=2)

    assert boot("dashboard").trading_env == "live"


def test_log_level_from_settings_hides_info_lines(env, clock_calls, capsys):
    env.setenv("LOG_LEVEL", "warning")

    boot("node")

    assert _lines(capsys) == []
