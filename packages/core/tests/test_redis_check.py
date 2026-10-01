"""covers: the trader refuses to start when Redis is down (a PING on a plain socket)."""

import socket
import threading

import pytest

from trading_core.redis_check import redis_reachable


@pytest.fixture
def fake_redis():
    """A one shot TCP server on localhost that answers the first request with ``reply``."""
    servers = []

    def start(reply):
        server = socket.create_server(("127.0.0.1", 0))
        servers.append(server)
        received = []

        def serve():
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(64))
                conn.sendall(reply)

        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1], received

    yield start
    for server in servers:
        server.close()


def test_pong_means_reachable(fake_redis):
    port, received = fake_redis(b"+PONG\r\n")

    assert redis_reachable("127.0.0.1", port) is True
    assert received == [b"PING\r\n"]


@pytest.mark.parametrize("reply", [b"-NOAUTH Authentication required.\r\n", b"", b"HTTP/1.1 400\r\n"])
def test_any_other_reply_is_not_reachable(fake_redis, reply):
    port, _ = fake_redis(reply)

    assert redis_reachable("127.0.0.1", port) is False


def test_closed_port_is_not_reachable():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    # The listener is closed now, so the connection is refused.

    assert redis_reachable("127.0.0.1", port, timeout=0.5) is False


def test_unknown_host_is_not_reachable():
    assert redis_reachable("no-such-host.invalid", 6379, timeout=0.5) is False
