"""Logs how far the trader's event loop is behind, and its memory, every 10 seconds (the OOM probe).

The demo trader was memory killed at its 1.5 GiB cap on 2026-11-20, 11-21, and twice on 11-23,
and its memory rises and falls instead of growing steadily. The lead is a backlog: when the loop
falls behind the Kraken quote and trade stream, NautilusTrader 1.231's ``ThrottledEnqueuer`` keeps
every message in memory, first as ready callbacks on the loop (``call_soon_threadsafe``), then,
past the data queue's capacity, as one pending ``put()`` task each. Its throttled
``at capacity`` warning undercounts that backlog, so this probe logs it directly.

Each line (code ``load_probe``) carries the loop's ready callbacks, the Nautilus engine queue
sizes, how late the probe's own sleep woke (``lag_s``), the process RSS, and the container's
cgroup memory. Reading fails soft: a missing value is logged as None and the probe carries on.
"""

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

PROBE_INTERVAL_S: Final = 10.0
PROC_STATUS: Final = Path("/proc/self/status")
CGROUP_MEMORY: Final = Path("/sys/fs/cgroup/memory.current")
_VMRSS = re.compile(r"^VmRSS:\s+(\d+)\s+kB$", re.MULTILINE)


def ready_callbacks(loop: asyncio.AbstractEventLoop) -> int | None:
    """The callbacks waiting to run on ``loop``, from CPython's private ``_ready`` deque; None if absent."""
    ready: object = getattr(loop, "_ready", None)
    return len(ready) if isinstance(ready, deque) else None


def rss_mib(status: Path = PROC_STATUS) -> int | None:
    """This process's resident memory in MiB, from ``/proc/self/status``; None when unreadable."""
    try:
        found = _VMRSS.search(status.read_text())
    except OSError:
        return None
    return int(found.group(1)) // 1024 if found else None


def cgroup_mib(current: Path = CGROUP_MEMORY) -> int | None:
    """The container's memory use in MiB, the figure the OOM killer compares to the cap; None when unreadable."""
    try:
        return int(current.read_text().strip()) // (1024 * 1024)
    except OSError, ValueError:
        return None


async def watch_load(
    loop: asyncio.AbstractEventLoop,
    queue_sizes: Callable[[], Mapping[str, int]],
    interval_s: float = PROBE_INTERVAL_S,
    monotonic: Callable[[], float] = time.monotonic,
    status: Path = PROC_STATUS,
    current: Path = CGROUP_MEMORY,
) -> None:
    """Log one ``load_probe`` line every ``interval_s`` until cancelled; ``queue_sizes`` names each engine queue."""
    lag_s = 0.0
    while True:
        try:
            queues = dict(queue_sizes())
        except Exception as exc:  # a diagnostic must never stop the trader; the error is logged
            queues = {}
            log.warning(
                "load probe could not read the queues",
                extra={"fields": {"code": "load_probe_failed", "error": type(exc).__name__}},
            )
        log.info(
            "load probe",
            extra={
                "fields": {
                    "code": "load_probe",
                    "ready": ready_callbacks(loop),
                    **queues,
                    "lag_s": round(lag_s, 3),
                    "rss_mib": rss_mib(status),
                    "cgroup_mib": cgroup_mib(current),
                }
            },
        )
        before = monotonic()
        await asyncio.sleep(interval_s)
        lag_s = max(0.0, monotonic() - before - interval_s)
