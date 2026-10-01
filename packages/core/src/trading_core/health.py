"""Container health check for Nautilus nodes: healthy when the heartbeat file is fresh.

Usage: ``python -m trading_core.health [path] [max_age_secs]``
"""

import sys
import time
from pathlib import Path

DEFAULT_PATH = "/run/heartbeat/alive"
DEFAULT_MAX_AGE_S = 30.0


def is_fresh(path: Path, max_age_s: float, now: float | None = None) -> bool:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return ((now if now is not None else time.time()) - mtime) < max_age_s


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    max_age = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MAX_AGE_S
    sys.exit(0 if is_fresh(path, max_age) else 1)


if __name__ == "__main__":
    main()
