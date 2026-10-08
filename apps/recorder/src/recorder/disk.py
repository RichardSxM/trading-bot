"""The recorder's hard disk floor (spec 0007 AC-7): it never fills the catalog's file system.

Below ``CATALOG_HARD_FLOOR_GB`` a running recorder stops with exit code 75, and a starting one
waits for space instead of building the node, so a full disk never turns into a crash loop.
Standard library only (the health check imports it); ``DiskGuardActor`` in ``actor.py`` runs the
guard inside the node.
"""

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

# The exit code of a recorder stopped by the floor (EX_TEMPFAIL): try again once there is space.
EXIT_DISK_FULL: Final = 75
# How long a clean shutdown may take before the process is ended anyway.
STOP_TIMEOUT_S: Final = 20.0
WAIT_EVERY_S: Final = 30.0

FreeBytes = Callable[[Path], int]


def free_bytes(path: Path) -> int:
    """Free space on ``path``'s file system, as a non root process can use it."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def wait_for_space(
    path: Path,
    floor_bytes: int,
    *,
    free: FreeBytes = free_bytes,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once ``path`` has ``floor_bytes`` free, checking every 30 s (AC-7, at start).

    Logs CRITICAL ``catalog_disk_full`` once when it has to wait, and INFO ``catalog_disk_ok``
    when space returns.
    """
    available = free(path)
    if available >= floor_bytes:
        return
    log.critical(
        "catalog disk below the hard floor, waiting for space before starting",
        extra={"fields": {"code": "catalog_disk_full", "free_bytes": available, "floor_bytes": floor_bytes}},
    )
    while available < floor_bytes:
        sleep(WAIT_EVERY_S)
        available = free(path)
    log.info(
        "catalog disk space is back, starting",
        extra={"fields": {"code": "catalog_disk_ok", "free_bytes": available, "floor_bytes": floor_bytes}},
    )


def stop_process() -> None:
    """Stop this process cleanly with ``SIGTERM`` (the node shuts down as on ``docker stop``),
    and end it with exit code 75 if that takes longer than ``STOP_TIMEOUT_S``."""
    fallback = threading.Timer(STOP_TIMEOUT_S, os._exit, (EXIT_DISK_FULL,))
    fallback.daemon = True
    fallback.start()
    os.kill(os.getpid(), signal.SIGTERM)


class DiskGuard:
    """The floor check without Nautilus: a free space reader and what to do when it trips."""

    def __init__(
        self, path: Path, floor_bytes: int, *, free: FreeBytes = free_bytes, stop: Callable[[], None] = stop_process
    ) -> None:
        self._path = path
        self._floor_bytes = floor_bytes
        self._free = free
        self._stop = stop
        self.tripped = False

    def check(self) -> None:
        """Below the floor: log CRITICAL ``catalog_disk_full``, flush the logs, and stop, once."""
        if self.tripped:
            return
        available = self._free(self._path)
        if available >= self._floor_bytes:
            return
        self.tripped = True
        log.critical(
            "catalog disk below the hard floor, stopping the recorder",
            extra={"fields": {"code": "catalog_disk_full", "free_bytes": available, "floor_bytes": self._floor_bytes}},
        )
        for handler in logging.getLogger().handlers:
            handler.flush()
        self._stop()
