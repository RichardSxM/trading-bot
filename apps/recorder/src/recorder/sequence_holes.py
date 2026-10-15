"""The recorder's sequence watch and its holes file (spec 0007 AC-15).

``SequenceWatch`` feeds every book delta's ``sequence`` and ``ts_init`` to the pure
``SequenceTracker``, logs each confirmed hole, and writes ``live/<run>/sequence_holes.jsonl`` once
per tick with one fsync: the holes confirmed since the last tick, then ``{"checked_to_ns": X}``.
The first line is ``{"started_ns": …}`` and, on a clean stop, the last is ``{"closed": true}``.
The compactor turns those holes into ``sequence`` gaps (AC-16). Without Nautilus, so tests drive
it directly; ``RecordingActor`` in ``actor.py`` runs it.

The tick runs on a Nautilus timer thread while ``observe_all`` runs on the event loop, so one lock
covers each whole method (spec 0012 AC-10).
"""

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final, Protocol

from recorder.disk import stop_process
from trading_core.marketdata.sequence import SequenceHole, SequenceTracker

log = logging.getLogger(__name__)

HOLES_FILE: Final = "sequence_holes.jsonl"


class Delta(Protocol):
    """What the watch reads from a book delta (a Nautilus ``OrderBookDelta`` in the recorder)."""

    @property
    def sequence(self) -> int: ...

    @property
    def ts_init(self) -> int: ...


class HolesFile:
    """Appends whole lines to one run's holes file, each batch with one write and one fsync."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, lines: list[Mapping[str, object]]) -> None:
        """Write ``lines`` as JSON lines and fsync them. Raises ``OSError`` when that fails.

        A short write is continued until every byte is written, so a line is never left torn while
        the recorder keeps running (a torn line would swallow the next one).
        """
        data = memoryview("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            os.close(fd)


class SequenceWatch:
    """The tracker, the holes file, and the recorder logs of AC-15.

    A failed write logs CRITICAL ``sequence_file_failed`` and stops the recorder through ``stop``
    (exit code 75, as the disk guard does), because a run without its holes file would hold back
    every later hour in the compactor. ``tripped`` tells ``main.py`` to exit 75.

    Every public method holds one lock for its whole body, and ``stop`` is called only after it is
    released. After ``stop`` the watch is closed: ``tick``, ``observe``, and ``stop`` again do nothing,
    so ``{"closed": true}`` is the file's last line, written once (spec 0012 AC-10).
    """

    def __init__(
        self,
        path: Callable[[], Path],
        *,
        confirm_ns: int,
        stop: Callable[[], None] = stop_process,
    ) -> None:
        self._path = path
        self._confirm_ns = confirm_ns
        self._stop = stop
        self._tracker = SequenceTracker(confirm_ns=confirm_ns)
        self._file: HolesFile | None = None
        self._late = 0
        self._late_first: int | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.tripped = False

    @property
    def run(self) -> str:
        """The run folder the holes file is in, once started."""
        return "" if self._file is None else self._file.path.parent.name

    def start(self, now_ns: int) -> None:
        """Before the subscriptions: a fresh tracker, and the file's ``started_ns`` line."""
        with self._lock:
            self._tracker = SequenceTracker(confirm_ns=self._confirm_ns)
            self._closed = False
            self._file = HolesFile(self._path())
            failed = self._write([{"started_ns": now_ns}])
        self._stop_if(failed)

    def observe(self, sequence: int, ts_init: int, now_ns: int) -> None:
        """One delta, in arrival order."""
        with self._lock:
            if not self._closed:
                self._observe(sequence, ts_init, now_ns)

    def observe_all(self, deltas: Iterable[Delta], now_ns: int) -> None:
        """Every delta of one message, in arrival order, under one lock."""
        with self._lock:
            if self._closed:
                return
            for delta in deltas:
                self._observe(delta.sequence, delta.ts_init, now_ns)

    def tick(self, now_ns: int) -> None:
        """Confirm holes, log them, and write them with the new ``checked_to_ns``."""
        with self._lock:
            if self._closed:
                return
            result = self._tracker.tick(now_ns)
            self._log_holes(result.holes)
            self._log_late()
            lines: list[Mapping[str, object]] = [h.to_dict() for h in result.holes]
            lines.append({"checked_to_ns": result.checked_to_ns})
            failed = self._write(lines)
        self._stop_if(failed)

    def stop(self) -> None:
        """At stop, every open suspicion is a hole (fail safe), then ``closed``."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            holes = self._tracker.close()
            self._log_holes(holes)
            self._log_late()
            lines: list[Mapping[str, object]] = [h.to_dict() for h in holes]
            lines.append({"closed": True})
            failed = self._write(lines)
        self._stop_if(failed)

    def _observe(self, sequence: int, ts_init: int, now_ns: int) -> None:
        if self._tracker.observe(sequence, ts_init, now_ns):
            self._late += 1
            if self._late_first is None:
                self._late_first = sequence

    def _log_holes(self, holes: list[SequenceHole]) -> None:
        for hole in holes:
            log.error(
                "book sequence hole",
                extra={
                    "fields": {
                        "code": "book_sequence_hole",
                        "run": self.run,
                        "first_missing": hole.first_missing,
                        "last_missing": hole.last_missing,
                        "missing": hole.missing,
                        "before_ns": hole.before_ns,
                        "after_ns": hole.after_ns,
                    }
                },
            )

    def _log_late(self) -> None:
        """One WARNING per tick for the numbers that came after their hole was confirmed."""
        if not self._late:
            return
        log.warning(
            "book sequence numbers arrived after their hole was confirmed",
            extra={
                "fields": {
                    "code": "book_sequence_late",
                    "run": self.run,
                    "first_sequence": self._late_first,
                    "count": self._late,
                }
            },
        )
        self._late, self._late_first = 0, None

    def _write(self, lines: list[Mapping[str, object]]) -> bool:
        """Append ``lines``; ``True`` when this write failed and tripped the watch."""
        if self.tripped or self._file is None:
            return False
        try:
            self._file.append(lines)
        except OSError as exc:
            self.tripped = True
            log.critical(
                "sequence holes file failed, stopping the recorder",
                extra={"fields": {"code": "sequence_file_failed", "path": str(self._file.path), "error": str(exc)}},
            )
            for handler in logging.getLogger().handlers:
                handler.flush()
            return True
        return False

    def _stop_if(self, failed: bool) -> None:
        # Outside the lock, so a stop that comes back into the watch cannot deadlock.
        if failed:
            self._stop()
