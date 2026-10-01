"""covers: spec 0001 Clock check (startup SNTP offset, median, per process policy)."""

import logging
import socket
import struct
import time
from typing import ClassVar

import pytest

from trading_core.clock import ClockCheckFailed, ClockResult, enforce, measure, sntp_offset_ms

LIMIT = 250.0


def _result(offset_ms, answered=3, queried=3):
    return ClockResult(offset_ms=offset_ms, answered=answered, queried=queried)


# enforce: the startup policy


@pytest.mark.parametrize("env", ["demo", "live"])
@pytest.mark.parametrize("offset", [251.0, -251.0])
def test_node_over_the_limit_refuses_in_every_env(env, offset):
    with pytest.raises(ClockCheckFailed, match="exceeds limit"):
        enforce(_result(offset), kind="node", env=env, max_offset_ms=LIMIT)


def test_node_with_no_answer_refuses_in_live():
    with pytest.raises(ClockCheckFailed, match="no clock server answered"):
        enforce(_result(None, answered=0), kind="node", env="live", max_offset_ms=LIMIT)


def test_node_with_no_answer_warns_and_starts_in_demo(caplog):
    with caplog.at_level(logging.INFO, logger="trading_core.clock"):
        enforce(_result(None, answered=0), kind="node", env="demo", max_offset_ms=LIMIT)

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "clock check: no server answered, starting anyway")
    ]


@pytest.mark.parametrize("offset", [0.0, 249.9, LIMIT, -LIMIT])
def test_node_within_or_exactly_at_the_limit_passes_at_info(caplog, offset):
    with caplog.at_level(logging.INFO, logger="trading_core.clock"):
        enforce(_result(offset), kind="node", env="live", max_offset_ms=LIMIT)

    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert record.getMessage() == "clock check passed"
    assert record.fields == {"offset_ms": offset, "answered": 3, "queried": 3, "max_offset_ms": LIMIT}


@pytest.mark.parametrize("env", ["demo", "live"])
@pytest.mark.parametrize(
    "result",
    [_result(None, answered=0), _result(5_000.0), _result(10.0)],
    ids=["no-answer", "over-limit", "within-limit"],
)
def test_dashboard_always_starts_and_logs_a_warning(caplog, env, result):
    with caplog.at_level(logging.INFO, logger="trading_core.clock"):
        enforce(result, kind="dashboard", env=env, max_offset_ms=LIMIT)

    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# measure: query every server, median of those that answer


def test_measure_takes_the_median_of_answering_servers():
    offsets = {"a": 10.0, "b": 300.0, "c": 20.0}

    result = measure(["a", "b", "c"], query=offsets.__getitem__)

    assert result == ClockResult(offset_ms=20.0, answered=3, queried=3)


def test_measure_skips_servers_that_fail_and_one_answer_is_enough(caplog):
    def query(server):
        if server == "timeout":
            raise OSError("timed out")
        if server == "garbled":
            raise ValueError("short SNTP reply")
        return 42.0

    with caplog.at_level(logging.WARNING, logger="trading_core.clock"):
        result = measure(["timeout", "ok", "garbled"], query=query)

    assert result == ClockResult(offset_ms=42.0, answered=1, queried=3)
    assert [r.fields["server"] for r in caplog.records] == ["timeout", "garbled"]


def test_measure_with_no_answer_reports_none():
    def query(server):
        raise OSError("unreachable")

    assert measure(["a", "b"], query=query) == ClockResult(offset_ms=None, answered=0, queried=2)


def test_measure_with_no_servers_reports_none():
    assert measure([], query=lambda s: 0.0) == ClockResult(offset_ms=None, answered=0, queried=0)


def test_measure_does_not_swallow_unexpected_errors():
    def query(server):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        measure(["a"], query=query)


# sntp_offset_ms: the wire format, with a fake UDP socket and a fixed local clock


class _FakeSocket:
    reply = b""
    sent: ClassVar[list[tuple[bytes, tuple[str, int]]]] = []

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, packet, address):
        _FakeSocket.sent.append((bytes(packet), address))

    def recvfrom(self, size):
        return _FakeSocket.reply, ("server", 123)


def _ntp(ts):
    whole = int(ts) + 2_208_988_800
    return whole, int((ts % 1) * 2**32)


def _reply(server_receive, server_transmit):
    data = bytearray(48)
    struct.pack_into("!II", data, 32, *_ntp(server_receive))
    struct.pack_into("!II", data, 40, *_ntp(server_transmit))
    return bytes(data)


@pytest.fixture
def fake_sntp(monkeypatch):
    _FakeSocket.sent = []
    monkeypatch.setattr(socket, "socket", _FakeSocket)

    def use(reply, local_times):
        _FakeSocket.reply = reply
        times = iter(local_times)
        monkeypatch.setattr(time, "time", lambda: next(times))

    return use


def test_sntp_offset_is_positive_when_the_local_clock_is_behind(fake_sntp):
    # Local send at T, local receive at T+0.2; the server, 500 ms ahead, answers at T+0.1 local.
    t = 1_790_000_000.0
    fake_sntp(_reply(t + 0.6, t + 0.6), [t, t + 0.2])

    assert sntp_offset_ms("pool.example") == pytest.approx(500.0, abs=0.01)


def test_sntp_offset_is_negative_when_the_local_clock_is_ahead(fake_sntp):
    t = 1_790_000_000.0
    fake_sntp(_reply(t - 0.2, t - 0.2), [t, t + 0.2])

    assert sntp_offset_ms("pool.example") == pytest.approx(-300.0, abs=0.01)


def test_sntp_sends_a_v4_client_request_to_port_123(fake_sntp):
    t = 1_790_000_000.0
    fake_sntp(_reply(t, t), [t, t])

    sntp_offset_ms("pool.example")

    ((packet, address),) = _FakeSocket.sent
    assert address == ("pool.example", 123)
    assert len(packet) == 48
    assert packet[0] == 0x23


def test_sntp_short_reply_raises_value_error(fake_sntp):
    t = 1_790_000_000.0
    fake_sntp(b"\x00" * 20, [t, t])

    with pytest.raises(ValueError, match=r"short SNTP reply from pool\.example"):
        sntp_offset_ms("pool.example")
