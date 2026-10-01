"""A dependency free Redis PING, used to refuse startup when Redis is down."""

import socket


def redis_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"PING\r\n")
            return sock.recv(64).startswith(b"+PONG")
    except OSError:
        return False
