"""The recorder's container health check (spec 0007 AC-7).

Healthy only while the heartbeat is under 30 s old, the current run's newest book delta file was
written under 60 s ago, and free space is at or above ``CATALOG_HARD_FLOOR_GB``. A node that runs
but records nothing, or a disk near full, shows as unhealthy.

Usage: ``python -m recorder.health``. It prints why it failed and exits 1.
"""

import os
import sys
import time
from pathlib import Path
from typing import Final

from recorder.disk import FreeBytes, free_bytes
from recorder.live_files import DELTAS_STREAM, current_run, discover
from recorder.settings import RecorderSettings
from trading_core.health import is_fresh
from trading_core.marketdata.hours import GB

HEARTBEAT_MAX_AGE_S: Final = 30.0
STREAM_MAX_AGE_S: Final = 60.0


def newest_delta_mtime(live: Path) -> float | None:
    """The newest modification time of the current run's ``order_book_deltas`` files, if any."""
    found = discover(live)
    run = current_run(found)
    times = [
        file.path.stat().st_mtime
        for (table, _), files in found.streams.items()
        if table == DELTAS_STREAM
        for file in files
        if file.run == run
    ]
    return max(times, default=None)


def check(
    catalog: Path,
    heartbeat: Path,
    floor_bytes: int,
    *,
    now: float | None = None,
    free: FreeBytes = free_bytes,
) -> str | None:
    """The reason the recorder is unhealthy, or ``None`` when all three conditions hold."""
    now = time.time() if now is None else now
    if not is_fresh(heartbeat, HEARTBEAT_MAX_AGE_S, now):
        return f"heartbeat {heartbeat} missing or older than {HEARTBEAT_MAX_AGE_S:.0f} s"
    written = newest_delta_mtime(catalog / "live")
    if written is None or now - written >= STREAM_MAX_AGE_S:
        return f"no {DELTAS_STREAM} file of the current run written in the last {STREAM_MAX_AGE_S:.0f} s"
    available = free(catalog)
    if available < floor_bytes:
        return f"free space {available} bytes is below the hard floor of {floor_bytes} bytes"
    return None


def main() -> None:
    settings = RecorderSettings()
    reason = check(
        Path(settings.catalog_path),
        Path(os.environ.get("HEARTBEAT_FILE", "/run/heartbeat/alive")),
        int(settings.catalog_hard_floor_gb * GB),
    )
    if reason is not None:
        print(f"unhealthy: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
