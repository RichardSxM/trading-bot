"""Free space on the host root filesystem (spec 0013 AC-4, *Value sourcing*).

``/var/log/kfs`` lives on the root filesystem, which also holds ``/var/lib/docker``, so its read
only mount is where the alerter measures it.
"""

import os
from pathlib import Path


def free_bytes(path: Path) -> int:
    """The bytes free to an unprivileged writer on ``path``'s filesystem. Raises ``OSError``."""
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize
