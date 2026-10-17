"""The host's own lines (spec 0013 AC-4, AC-11): ``host.jsonl`` and ``deploys.jsonl`` in the VPS's
``/var/log/kfs``, mounted read only.

Each file is followed from its end as the alerter first sees it, so a recreated alerter replays
nothing. Every deploy recreates it, though, so the first pass looks back once: a "deploy started"
line under 15 min old with no matching ``deploys.jsonl`` line yet opens the window again. A file
that shrinks (``copytruncate``) or is replaced (a new inode) is read again from its start; a line
still being written waits for its newline.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from trading_core.alerts.engine import DEPLOY_STARTED
from trading_core.alerts.model import Observation
from trading_core.alerts.rules import DEPLOY, HOST, text_field

log = logging.getLogger(__name__)

HOST_FILE: Final = "host.jsonl"
DEPLOYS_FILE: Final = "deploys.jsonl"
# At most this much is read per file per pass; the rest waits for the next pass.
MAX_READ: Final = 1 << 20
# How far back the first pass looks for a deploy still under way, per file.
TAIL_READ: Final = 64 << 10
# A deploy started this long ago is past its window (the engine's ``deploy_window_s``).
RESUME_S: Final = 900

Clock = Callable[[], datetime]


class FileFollower:
    """New complete lines of one append only file. Raises ``OSError`` on a read that fails."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset: int | None = None
        self._inode: int | None = None
        self._rest = b""
        # The complete lines before the end, as first seen; for the first pass's look back.
        self.tail: list[str] = []

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
            self.tail = self._tail(stat.st_size)
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

    def _tail(self, size: int) -> list[str]:
        start = max(0, size - TAIL_READ)
        with self._path.open("rb") as file:
            file.seek(start)
            chunk = file.read(size - start)
        lines = chunk.split(b"\n")[:-1]  # the last piece has no newline yet
        if start:
            lines = lines[1:]  # cut through the middle
        return [line.decode(errors="replace") for line in lines if line.strip()]


class HostFiles:
    """Both host files as ``Observation``s, ``host.jsonl`` before ``deploys.jsonl`` within a pass."""

    def __init__(self, directory: Path, clock: Clock, *, resume_s: int = RESUME_S) -> None:
        self._host = FileFollower(directory / HOST_FILE)
        self._deploys = FileFollower(directory / DEPLOYS_FILE)
        self._files = ((HOST, self._host), (DEPLOY, self._deploys))
        self._clock = clock
        self._resume_s = resume_s
        self._first = True
        self.ok_at: datetime | None = None

    def poll(self) -> list[Observation]:
        """The new lines of both files. Raises ``OSError``; ``ok_at`` moves only on a full pass (AC-2).

        The first pass gives at most the start line of a deploy still under way (see the module).
        """
        now = self._clock()
        observations = []
        for source, follower in self._files:
            for line in follower.read():
                observation = parse(source, line, now)
                if observation is not None:
                    observations.append(observation)
        if self._first:
            self._first = False
            resumed = self._deploy_under_way(now)
            if resumed is not None:
                observations.insert(0, resumed)
        self.ok_at = now
        return observations

    def _deploy_under_way(self, now: datetime) -> Observation | None:
        """The last "deploy started" line when under ``resume_s`` old and its outcome line is not written."""
        starts = [o for o in _parsed(HOST, self._host.tail, now) if text_field(o, "msg") == DEPLOY_STARTED]
        if not starts:
            return None
        start = starts[-1]
        began = _utc(text_field(start, "started_at")) or start.at
        if (now - began).total_seconds() >= self._resume_s:
            return None
        sha = text_field(start, "sha")
        for outcome in _parsed(DEPLOY, self._deploys.tail, now):
            ended = _utc(text_field(outcome, "started_at"))
            if text_field(outcome, "sha") == sha and ended is not None and ended >= began:
                return None
        log.info(
            "deploy under way at start, window opened",
            extra={"fields": {"code": "alerter_deploy_resumed", "sha": sha, "started_at": began.isoformat()}},
        )
        return start


def _parsed(source: str, lines: list[str], now: datetime) -> list[Observation]:
    return [o for o in (parse(source, line, now) for line in lines) if o is not None]


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
