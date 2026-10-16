"""The host's own lines (spec 0013 AC-4, AC-11): ``host.jsonl`` and ``deploys.jsonl`` in the VPS's
``/var/log/kfs``, mounted read only.

Each file is followed from its end as the alerter first sees it, so an alerter recreated in the
middle of a deploy replays nothing and opens no window. A file that shrinks (``copytruncate``)
or is replaced (a new inode) is read again from its start; a line still being written waits for
its newline.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from trading_core.alerts.model import Observation
from trading_core.alerts.rules import DEPLOY, HOST

log = logging.getLogger(__name__)

HOST_FILE: Final = "host.jsonl"
DEPLOYS_FILE: Final = "deploys.jsonl"
# At most this much is read per file per pass; the rest waits for the next pass.
MAX_READ: Final = 1 << 20

Clock = Callable[[], datetime]


class FileFollower:
    """New complete lines of one append only file. Raises ``OSError`` on a read that fails."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset: int | None = None
        self._inode: int | None = None
        self._rest = b""

    def read(self) -> list[str]:
        """The lines appended since the last read (none on the first)."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            # A file that appears later is new, so it is read from its start.
            self._offset, self._inode, self._rest = 0, None, b""
            return []
        if self._offset is None:
            self._offset, self._inode = stat.st_size, stat.st_ino
            return []
        if stat.st_ino != self._inode or stat.st_size < self._offset:
            self._offset, self._inode, self._rest = 0, stat.st_ino, b""
        if stat.st_size == self._offset:
            return []
        with self._path.open("rb") as file:
            file.seek(self._offset)
            chunk = file.read(MAX_READ)
        self._offset += len(chunk)
        *lines, rest = (self._rest + chunk).split(b"\n")
        # A "line" longer than a whole read is never JSON we act on; drop it rather than grow.
        self._rest = rest if len(rest) < MAX_READ else b""
        return [line.decode(errors="replace") for line in lines if line.strip()]


class HostFiles:
    """Both host files as ``Observation``s, ``host.jsonl`` before ``deploys.jsonl`` within a pass."""

    def __init__(self, directory: Path, clock: Clock) -> None:
        self._files = ((HOST, FileFollower(directory / HOST_FILE)), (DEPLOY, FileFollower(directory / DEPLOYS_FILE)))
        self._clock = clock
        self.ok_at: datetime | None = None

    def poll(self) -> list[Observation]:
        """The new lines of both files. Raises ``OSError``; ``ok_at`` moves only on a full pass (AC-2)."""
        now = self._clock()
        observations = []
        for source, follower in self._files:
            for line in follower.read():
                observation = parse(source, line, now)
                if observation is not None:
                    observations.append(observation)
        self.ok_at = now
        return observations


def parse(source: str, line: str, now: datetime) -> Observation | None:
    """One host line as an ``Observation`` (``code`` None: rules match its fields); None when not a JSON object.

    Its time is the line's ``ts`` (``host.jsonl``) or ``finished_at`` (``deploys.jsonl``), else ``now``.
    """
    try:
        body = json.loads(line)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        log.warning("host line not JSON", extra={"fields": {"code": "alerter_host_line_bad", "source": source}})
        return None
    level = body.get("level")
    stamp = body.get("ts") if source == HOST else body.get("finished_at")
    at = _utc(stamp) if isinstance(stamp, str) else None
    return Observation(
        source=source,
        service=source,
        at=at if at is not None and at <= now else now,
        code=None,
        level=level if isinstance(level, str) else None,
        fields=body,
    )


def _utc(text: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo is not None and at.utcoffset() == timedelta(0) else None
