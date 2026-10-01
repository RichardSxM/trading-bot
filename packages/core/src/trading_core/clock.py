"""Startup clock offset check over SNTP (stdlib only, no extra service).

Containers share the host kernel clock, so this measures the host. chrony keeps the VPS
clock right; this check does not read chrony (it is absent on the laptop and in containers).
Watching drift while running belongs to feature 12.
"""

import logging
import socket
import statistics
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from trading_core.settings import TradingEnv

log = logging.getLogger(__name__)

ProcessKind = Literal["node", "dashboard"]  # node = trader and recorder

_NTP_EPOCH_DELTA = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01
_NTP_PORT = 123
_TIMEOUT_S = 2.0


class ClockCheckFailed(RuntimeError):
    """The process must not start: the clock is off, or unverifiable in live."""


@dataclass(frozen=True)
class ClockResult:
    offset_ms: float | None  # median offset, None when no server answered
    answered: int
    queried: int


def _to_ntp(ts: float) -> tuple[int, int]:
    whole = int(ts) + _NTP_EPOCH_DELTA
    return whole, int((ts % 1) * 2**32)


def _from_ntp(whole: int, frac: int) -> float:
    return whole - _NTP_EPOCH_DELTA + frac / 2**32


def sntp_offset_ms(server: str, timeout: float = _TIMEOUT_S) -> float:
    """Offset of this host's clock versus ``server`` in ms (positive = we are behind)."""
    packet = bytearray(48)
    packet[0] = 0x23  # LI 0, version 4, mode 3 (client)
    t1 = time.time()
    struct.pack_into("!II", packet, 40, *_to_ntp(t1))  # transmit timestamp
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, _NTP_PORT))
        data, _ = sock.recvfrom(48)
    t4 = time.time()
    if len(data) < 48:
        raise ValueError(f"short SNTP reply from {server}")
    t2 = _from_ntp(*struct.unpack_from("!II", data, 32))  # server receive
    t3 = _from_ntp(*struct.unpack_from("!II", data, 40))  # server transmit
    return ((t2 - t1) + (t3 - t4)) / 2 * 1000


def measure(
    servers: Iterable[str],
    query: Callable[[str], float] = sntp_offset_ms,
) -> ClockResult:
    servers = list(servers)
    offsets: list[float] = []
    for server in servers:
        try:
            offsets.append(query(server))
        except (OSError, ValueError) as exc:
            log.warning("clock server did not answer", extra={"fields": {"server": server, "error": str(exc)}})
    offset = statistics.median(offsets) if offsets else None
    return ClockResult(offset_ms=offset, answered=len(offsets), queried=len(servers))


def enforce(result: ClockResult, kind: ProcessKind, env: TradingEnv, max_offset_ms: float) -> None:
    """Apply the spec 0001 startup policy to a measurement. Raises ClockCheckFailed to refuse."""
    fields = {
        "offset_ms": result.offset_ms,
        "answered": result.answered,
        "queried": result.queried,
        "max_offset_ms": max_offset_ms,
    }
    if result.offset_ms is None:
        if kind == "node" and env == "live":
            raise ClockCheckFailed("no clock server answered; refusing to start in live")
        log.warning("clock check: no server answered, starting anyway", extra={"fields": fields})
        return
    if abs(result.offset_ms) > max_offset_ms:
        if kind == "node":
            raise ClockCheckFailed(f"clock offset {result.offset_ms:.1f} ms exceeds limit {max_offset_ms} ms")
        log.warning("clock check: offset over limit, dashboard starts anyway", extra={"fields": fields})
        return
    level = logging.WARNING if kind == "dashboard" else logging.INFO
    log.log(level, "clock check passed", extra={"fields": fields})
