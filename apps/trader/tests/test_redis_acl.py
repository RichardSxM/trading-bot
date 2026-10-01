"""covers: spec 0003 AC-10, what the `trader` Redis user and an unauthenticated client may do.

Needs the Compose Redis and the trader's mounted password:
``docker compose run --rm -T trader pytest -q -m redis``.
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
def anonymous() -> Iterator[Resp]:
    client = Resp()
    yield client
    client.close()


@pytest.fixture
def trader() -> Iterator[Resp]:
    client = Resp()
    password = read_secret(REDIS_PASSWORD).get_secret_value()
    assert client("AUTH", "trader", password) == "OK"
    yield client
    client("DEL", "trader-ACLTEST:probe", "acltest:control")
    client.close()


def test_without_a_login_only_ping_is_allowed(anonymous):
    assert anonymous("PING") == "PONG"
    reply = anonymous("GET", "trader-ACLTEST:probe")
    assert isinstance(reply, Error)
    assert reply.startswith(("NOPERM", "NOAUTH"))


def test_a_wrong_password_is_refused(anonymous):
    reply = anonymous("AUTH", "trader", "not-the-password-0000")
    assert isinstance(reply, Error)
    assert reply.startswith("WRONGPASS")


def test_trader_reads_and_writes_its_keys(trader):
    assert trader("SET", "trader-ACLTEST:probe", "1") == "OK"
    assert trader("GET", "trader-ACLTEST:probe") == "1"


@pytest.mark.parametrize("command", [("FLUSHALL",), ("FLUSHDB",), ("CONFIG", "GET", "maxmemory"), ("ACL", "LIST")])
def test_trader_may_not_flush_or_administer(trader, command):
    reply = trader(*command)
    assert isinstance(reply, Error)
    assert reply.startswith("NOPERM")
