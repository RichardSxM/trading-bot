"""covers: spec 0005 AC-4, the deploy's health gate reads the trader heartbeat (and its git_commit)
through ``python -m dashboard.control_cli heartbeat``: one JSON line, exit 0 only when fresh.
spec 0005 AC-8: ``state``, ``pause``/``unpause`` with their result, and ``wait-flat``."""

import asyncio
import itertools
import json
from datetime import UTC, datetime

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard import control_cli
from dashboard.control_cli import parse_duration, read_snapshot, send_and_wait, snapshot_result, wait_flat
from tests.conftest import FakeRedis
from trading_core.control.messages import ControlResult, command, from_fields, result_to_fields
from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource
from trading_core.snapshot.envelope import encode

KEY = "demo:snapshot:heartbeat"
SECOND = 1_000_000_000


def test_fresh_heartbeat_passes_with_its_data(clock):
    raw = encode(clock.ns() - 2 * SECOND, {"git_commit": "abc"})
    result = snapshot_result(raw, KEY, clock.ns())
    assert result.code == 0
    assert result.line["status"] == "fresh"
    assert result.line["data"] == {"git_commit": "abc"}
    assert result.line["age_s"] == 2.0


def test_heartbeat_older_than_five_seconds_is_stale(clock):
    result = snapshot_result(encode(clock.ns() - 6 * SECOND, {}), KEY, clock.ns())
    assert (result.code, result.line["status"]) == (1, "stale")


def test_heartbeat_slightly_from_the_future_is_fresh(clock):
    result = snapshot_result(encode(clock.ns() + SECOND, {}), KEY, clock.ns())
    assert (result.code, result.line["status"]) == (0, "fresh")


def test_missing_heartbeat_fails(clock):
    result = snapshot_result(None, KEY, clock.ns())
    assert result.code == 1
    assert result.line == {"key": KEY, "status": "missing"}


def test_malformed_heartbeat_fails(clock):
    result = snapshot_result("not json", KEY, clock.ns())
    assert (result.code, result.line["status"]) == (1, "malformed")


def test_unreachable_redis_fails(clock):
    fake = FakeRedis()
    fake.fail = RedisConnectionError("down")
    result = asyncio.run(read_snapshot(fake, KEY, clock.ns))
    assert result.code == 1
    assert result.line == {"key": KEY, "status": "unreachable", "error": "ConnectionError"}


def _secrets_dir(tmp_path, monkeypatch):
    (tmp_path / "redis_password").write_text("r" * 32)
    monkeypatch.setenv("SECRETS_DIR_IN_CONTAINER", str(tmp_path))
    monkeypatch.setenv("TRADING_ENV", "demo")


def test_main_prints_one_json_line_and_exits_zero(tmp_path, monkeypatch, capsys, redis):
    _secrets_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(control_cli, "redis_client", lambda settings, password: redis)
    assert control_cli.main(["heartbeat"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    line = json.loads(out[0])
    assert line["status"] == "fresh"
    assert line["data"] == {"process": "trader"}


def test_main_reads_the_env_prefixed_key(tmp_path, monkeypatch, capsys):
    _secrets_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "true")
    fake = FakeRedis()
    monkeypatch.setattr(control_cli, "redis_client", lambda settings, password: fake)
    assert control_cli.main(["heartbeat"]) == 1
    assert json.loads(capsys.readouterr().out) == {"key": "live:snapshot:heartbeat", "status": "missing"}


def test_main_without_the_redis_password_fails_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRETS_DIR_IN_CONTAINER", str(tmp_path))
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert control_cli.main(["heartbeat"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["reason"] == "missing"


def test_main_refuses_bad_config(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_ENV", "nonsense")
    assert control_cli.main(["heartbeat"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "invalid configuration"


# spec 0005 AC-8: pause, unpause, state, wait-flat ----------------------------------------

STATE_KEY = "demo:snapshot:trader_state"


class AnsweringRedis(FakeRedis):
    """A trader stand in: answers every command appended to the control stream with ``answers``."""

    def __init__(self, *answers, reason=None):
        super().__init__()
        self.answers = answers
        self.reason = reason

    async def xadd(self, name, fields, id="*", maxlen=None, approximate=True):
        entry_id = await super().xadd(name, fields, id, maxlen, approximate)
        if name == "demo:control":
            msg = from_fields({k: str(v) for k, v in fields.items()})
            for outcome in self.answers:
                reason = self.reason if outcome is ControlOutcome.FAILED else None
                result = ControlResult(msg.id, outcome, msg.issued_at, reason)
                self.append("demo:control:results", result_to_fields(result))
        return entry_id


def _monotonic(step=1.0):
    counter = itertools.count()
    return lambda: next(counter) * step


def _pause():
    return command(ControlCmd.PAUSE, datetime.now(UTC), 60, ControlSource.DEPLOY)


def test_pause_appends_one_command_with_its_source_and_ttl_and_exits_0_on_executed():
    redis = AnsweringRedis(ControlOutcome.RECEIVED, ControlOutcome.EXECUTED)
    msg = _pause()

    result = asyncio.run(send_and_wait(redis, msg, "demo", 30, poll_s=0))

    assert result.code == 0
    assert result.line == {"id": str(msg.id), "cmd": "pause", "source": "deploy", "status": "executed"}
    [(_, fields)] = redis.streams["demo:control"]
    sent = from_fields(fields)
    assert (sent.cmd, sent.source) == (ControlCmd.PAUSE, ControlSource.DEPLOY)
    assert (sent.expires_at - sent.issued_at).total_seconds() == 60
    assert redis.xadd_calls[0]["maxlen"] == 1000


@pytest.mark.parametrize("outcome", [ControlOutcome.FAILED, ControlOutcome.EXPIRED, ControlOutcome.DUPLICATE])
def test_any_other_final_outcome_exits_1(outcome):
    redis = AnsweringRedis(ControlOutcome.RECEIVED, outcome, reason="pause_key_write_failed")

    result = asyncio.run(send_and_wait(redis, _pause(), "demo", 30, poll_s=0))

    assert result.code == 1
    assert result.line["status"] == outcome.value
    if outcome is ControlOutcome.FAILED:
        assert result.line["reason"] == "pause_key_write_failed"


def test_received_alone_is_not_an_answer_and_times_out():
    redis = AnsweringRedis(ControlOutcome.RECEIVED)

    result = asyncio.run(send_and_wait(redis, _pause(), "demo", 30, monotonic=_monotonic(10), poll_s=0))

    assert (result.code, result.line["status"]) == (1, "no_result")


def test_a_redis_that_refuses_the_append_gives_not_sent():
    redis = AnsweringRedis()
    redis.fail = RedisConnectionError("down")

    result = asyncio.run(send_and_wait(redis, _pause(), "demo", 30, poll_s=0))

    assert result.code == 1
    assert result.line["status"] == "not_sent"
    assert result.line["error"] == "ConnectionError"


def test_results_of_other_commands_are_ignored():
    redis = AnsweringRedis()
    other = _pause()
    redis.append(
        "demo:control:results", result_to_fields(ControlResult(other.id, ControlOutcome.EXECUTED, other.issued_at))
    )

    result = asyncio.run(send_and_wait(redis, _pause(), "demo", 30, monotonic=_monotonic(10), poll_s=0))

    assert result.line["status"] == "no_result"


class StateSequence(FakeRedis):
    """Returns one trader_state snapshot per read, written ``apart_s`` after the last."""

    def __init__(self, clock, states, apart_s=1.0):
        super().__init__()
        self.clock = clock
        self.states = list(states)
        self.apart_ns = int(apart_s * SECOND)
        self.ts = clock.ns()

    async def get(self, name):
        self.reads += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state is None:
            return None
        self.ts += self.apart_ns
        self.clock.now = self.ts / SECOND  # each read is fresh
        return encode(self.ts, state)


FLAT = {"paused": True, "open_positions": 0, "open_entry_orders": 0}
BUSY = {"paused": True, "open_positions": 1, "open_entry_orders": 0}


def test_wait_flat_needs_two_flat_reads_at_least_two_seconds_apart(clock):
    redis = StateSequence(clock, [FLAT, FLAT, FLAT])

    result = asyncio.run(wait_flat(redis, STATE_KEY, 900, clock.ns, _monotonic(), poll_s=0))

    assert (result.code, result.line["status"]) == (0, "flat")
    assert redis.reads == 3  # 1 s apart: the third read is the first 2 s after the first


def test_a_busy_read_restarts_the_count(clock):
    redis = StateSequence(clock, [FLAT, BUSY, FLAT, FLAT, FLAT], apart_s=2.0)

    result = asyncio.run(wait_flat(redis, STATE_KEY, 900, clock.ns, _monotonic(), poll_s=0))

    assert result.code == 0
    assert redis.reads == 4


def test_wait_flat_times_out_with_exit_2_and_the_counts(clock):
    redis = StateSequence(clock, [BUSY])

    result = asyncio.run(wait_flat(redis, STATE_KEY, 5, clock.ns, _monotonic(), poll_s=0))

    assert result.code == 2
    assert result.line["status"] == "not_flat"
    assert result.line["open_positions"] == 1


def test_a_lifted_pause_ends_the_wait_with_exit_3(clock):
    redis = StateSequence(clock, [BUSY, BUSY | {"paused": False}])

    result = asyncio.run(wait_flat(redis, STATE_KEY, 900, clock.ns, _monotonic(), poll_s=0))

    assert (result.code, result.line["status"]) == (3, "pause_lifted")


def test_a_missing_trader_state_ends_the_wait_with_exit_1(clock):
    # A release from before spec 0005 publishes no trader_state: the deploy fails closed.
    redis = StateSequence(clock, [None])

    result = asyncio.run(wait_flat(redis, STATE_KEY, 900, clock.ns, _monotonic(), poll_s=0))

    assert (result.code, result.line["status"]) == (1, "missing")


def test_a_stale_trader_state_ends_the_wait_with_exit_1(clock, redis):
    redis.values[STATE_KEY] = encode(clock.ns() - 6 * SECOND, FLAT)

    result = asyncio.run(wait_flat(redis, STATE_KEY, 900, clock.ns, _monotonic(), poll_s=0))

    assert (result.code, result.line["status"]) == (1, "stale")


@pytest.mark.parametrize(("text", "seconds"), [("90", 90), ("90s", 90), ("15m", 900), ("1h", 3600)])
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "15x", "-5", "1.5m"])
def test_bad_durations_are_refused(text):
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration(text)


def test_main_state_reads_the_trader_state_key(tmp_path, monkeypatch, capsys, redis, clock):
    _secrets_dir(tmp_path, monkeypatch)
    redis.values[STATE_KEY] = encode(clock.ns(), FLAT)
    monkeypatch.setattr(control_cli, "redis_client", lambda settings, password: redis)

    assert control_cli.main(["state"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == FLAT


def test_main_pause_sends_from_deploy_by_default(tmp_path, monkeypatch, capsys):
    _secrets_dir(tmp_path, monkeypatch)
    redis = AnsweringRedis(ControlOutcome.RECEIVED, ControlOutcome.EXECUTED)
    monkeypatch.setattr(control_cli, "redis_client", lambda settings, password: redis)

    assert control_cli.main(["pause", "--wait-result", "5"]) == 0
    line = json.loads(capsys.readouterr().out)
    assert (line["cmd"], line["source"], line["status"]) == ("pause", "deploy", "executed")
