"""covers: spec 0001 and 0003 boot order (settings, logs, env guard, secrets, config guard, clock) and refusals."""

import json
from uuid import uuid4

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
    settings = boot("node").settings

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

    assert isinstance(boot("node", settings_cls=AppSettings).settings, AppSettings)


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

    def guard(settings, secrets):
        seen.append(settings.trading_env)
        raise EnvGuardRefused("trader in demo may not build execution client(s) ['KRAKEN']")

    with pytest.raises(SystemExit) as exit_info:
        boot("node", guard=guard)

    assert exit_info.value.code == 1
    assert seen == ["demo"]
    assert clock_calls == []  # covers: guard runs before the clock check
    lines = _lines(capsys)
    assert [line["msg"] for line in lines] == ["starting", "env guard refused, refusing to start"]
    assert lines[-1]["level"] == "ERROR"
    assert lines[-1]["code"] == "env_guard_refused"
    assert lines[-1]["reason"] == "trader in demo may not build execution client(s) ['KRAKEN']"


def test_guard_that_passes_lets_boot_continue_to_the_clock_check(env, clock_calls):
    boot("node", guard=lambda settings, secrets: None)

    assert len(clock_calls) == 1


def test_errors_other_than_a_refusal_are_not_swallowed_by_boot(env, clock_calls):
    def guard(settings, secrets):
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

    assert boot("dashboard").settings.trading_env == "live"


def test_log_level_from_settings_hides_info_lines(env, clock_calls, capsys):
    env.setenv("LOG_LEVEL", "warning")

    boot("node")

    assert _lines(capsys) == []


# Generated per run, so no literal ever looks like a key to the secret scanner.
SECRET = uuid4().hex


def test_kraken_key_variable_refuses_before_secrets_are_read(env, clock_calls, capsys):
    env.setenv("KRAKEN_FUTURES_API_KEY", "x")

    with pytest.raises(SystemExit) as exit_info:
        boot("node", secrets=("postgres_password",))  # the file does not exist; never reached

    assert exit_info.value.code == 1
    assert clock_calls == []
    lines = _lines(capsys)
    assert [line["msg"] for line in lines] == ["starting", "env guard refused, refusing to start"]
    assert "KRAKEN_FUTURES_API_KEY" in lines[-1]["reason"]


def test_secrets_are_loaded_logged_and_handed_to_the_guard(env, clock_calls, capsys, secret_files):
    secret_files(postgres_password=SECRET)
    seen = {}

    def guard(settings, secrets):
        seen.update(secrets)

    result = boot("node", guard=guard, secrets=("postgres_password",))

    assert result.secrets["postgres_password"].get_secret_value() == SECRET
    assert set(seen) == {"postgres_password"}
    lines = _lines(capsys)
    assert [line["msg"] for line in lines] == ["starting", "secret loaded", "clock check passed"]
    assert lines[1]["secret"] == "postgres_password"


@pytest.mark.parametrize(("content", "reason"), [(None, "missing"), ("", "empty"), ("abc", "too_short")])
def test_an_unusable_secret_exits_1_with_one_critical_line_and_no_value(
    env, clock_calls, capsys, secret_files, content, reason
):
    folder = secret_files()
    if content is not None:
        (folder / "postgres_password").write_text(content)
    guard_calls = []

    with pytest.raises(SystemExit) as exit_info:
        boot("node", guard=lambda s, sec: guard_calls.append(1), secrets=("postgres_password",))

    assert exit_info.value.code == 1
    assert (guard_calls, clock_calls) == ([], [])
    line = _lines(capsys)[-1]
    assert (line["level"], line["code"], line["secret"], line["reason"]) == (
        "CRITICAL",
        "secret_unavailable",
        "postgres_password",
        reason,
    )
    assert line["env"] == "demo"


def test_secret_names_may_depend_on_the_settings(env, clock_calls, secret_files):
    secret_files(postgres_password=SECRET, kraken_api_key=SECRET[::-1])
    env.setenv("TRADING_ENV", "live")
    clock_calls.result = ClockResult(offset_ms=12.0, answered=2, queried=2)

    def names(settings):
        return ("postgres_password", "kraken_api_key") if settings.trading_env == "live" else ("postgres_password",)

    assert set(boot("node", secrets=names).secrets) == {"postgres_password", "kraken_api_key"}
