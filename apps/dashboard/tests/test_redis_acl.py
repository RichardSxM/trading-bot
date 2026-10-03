"""covers: spec 0003 AC-10 and spec 0004 AC-14, the `dashboard` Redis user reads snapshots, reads and appends
to control, reads control results, and nothing else.

Needs the Compose Redis and the dashboard's mounted password:
``docker compose run --rm -T dashboard pytest -q -m redis``.
"""

import os
import socket
from collections.abc import Iterator

import pytest

from trading_core.secrets import REDIS_PASSWORD, read_secret

pytestmark = pytest.mark.redis

HOST = os.environ.get("REDIS_HOST", "redis")
PORT = int(os.environ.get("REDIS_PORT", "6379"))


class Error(str):
    """A Redis error reply."""


class Resp:
    """The smallest RESP2 client that can send a command and read its reply."""

    def __init__(self) -> None:
        self.sock = socket.create_connection((HOST, PORT), timeout=5)
        self.file = self.sock.makefile("rb")

    def __call__(self, *args: str) -> object:
        frame = f"*{len(args)}\r\n" + "".join(f"${len(a.encode())}\r\n{a}\r\n" for a in args)
        self.sock.sendall(frame.encode())
        return self._read()

    def _read(self) -> object:
        line = self.file.readline().rstrip(b"\r\n")
        kind, rest = line[:1], line[1:].decode()
        if kind == b"+":
            return rest
        if kind == b"-":
            return Error(rest)
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            return None if size < 0 else self.file.read(size + 2)[:-2].decode()
        if kind == b"*":
            size = int(rest)
            return None if size < 0 else [self._read() for _ in range(size)]
        raise AssertionError(f"unexpected reply {line!r}")

    def close(self) -> None:
        self.file.close()
        self.sock.close()


@pytest.fixture
def dashboard() -> Iterator[Resp]:
    client = Resp()
    password = read_secret(REDIS_PASSWORD).get_secret_value()
    assert client("AUTH", "dashboard", password) == "OK"
    yield client
    client.close()


def test_dashboard_reads_snapshot_keys(dashboard):
    assert dashboard("GET", "acltest:snapshot:probe") is None


def test_dashboard_appends_to_the_control_stream(dashboard):
    # A throwaway stream that matches *:control; the trader's ACL test deletes it.
    reply = dashboard("XADD", "acltest:control", "*", "probe", "1")
    assert isinstance(reply, str)
    assert not isinstance(reply, Error)


@pytest.mark.parametrize(
    "command",
    [
        ("XRANGE", "acltest:control", "-", "+"),
        ("XREVRANGE", "acltest:control", "+", "-", "COUNT", "1"),
        ("XREAD", "COUNT", "1", "STREAMS", "acltest:control", "0"),
        ("XRANGE", "acltest:control:results", "-", "+"),
        ("XREVRANGE", "acltest:control:results", "+", "-", "COUNT", "200"),
        ("XREAD", "COUNT", "1", "STREAMS", "acltest:control:results", "0"),
    ],
)
def test_dashboard_reads_the_control_and_result_streams(dashboard, command):
    reply = dashboard(*command)
    assert not isinstance(reply, Error), reply


@pytest.mark.parametrize(
    "command",
    [
        ("SET", "acltest:snapshot:probe", "1"),
        ("DEL", "acltest:snapshot:probe"),
        ("SET", "acltest:control", "1"),
        ("DEL", "acltest:control"),
        ("XADD", "acltest:control:results", "*", "probe", "1"),
        ("XTRIM", "acltest:control", "MAXLEN", "0"),
        ("GET", "trader-ACLTEST:probe"),
        # The pause key is the trader's alone (spec 0005 AC-6); the dashboard pauses through the stream.
        ("SET", "acltest:trader:pause", "1"),
        ("DEL", "acltest:trader:pause"),
        ("XADD", "acltest:other", "*", "probe", "1"),
        ("FLUSHDB",),
    ],
)
def test_dashboard_is_refused_everything_else(dashboard, command):
    reply = dashboard(*command)
    assert isinstance(reply, Error)
    assert reply.startswith("NOPERM")
