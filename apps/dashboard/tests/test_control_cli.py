"""covers: spec 0005 AC-4, the deploy's health gate reads the trader heartbeat (and its git_commit)
through ``python -m dashboard.control_cli heartbeat``: one JSON line, exit 0 only when fresh."""

import asyncio
import json

from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard import control_cli
from dashboard.control_cli import read_snapshot, snapshot_result
from tests.conftest import FakeRedis
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
