"""The recorder's sequence watch and its holes file (spec 0007 AC-15).

``SequenceWatch`` feeds every book delta's ``sequence`` and ``ts_init`` to the pure
``SequenceTracker``, logs each confirmed hole, and writes ``live/<run>/sequence_holes.jsonl`` once
per tick with one fsync: the holes confirmed since the last tick, then ``{"checked_to_ns": X}``.
The first line is ``{"started_ns": …}`` and, on a clean stop, the last is ``{"closed": true}``.
The compactor turns those holes into ``sequence`` gaps (AC-16). Without Nautilus, so tests drive
it directly; ``RecordingActor`` in ``actor.py`` runs it.
"""

import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from recorder.disk import stop_process
from trading_core.marketdata.sequence import SequenceHole, SequenceTracker

log = logging.getLogger(__name__)

HOLES_FILE: Final = "sequence_holes.jsonl"


class HolesFile:
    """Appends whole lines to one run's holes file, each batch with one write and one fsync."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, lines: list[Mapping[str, object]]) -> None:
        """Write ``lines`` as JSON lines and fsync them. Raises ``OSError`` when that fails."""
        data = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)


class SequenceWatch:
    """The tracker, the holes file, and the recorder logs of AC-15.

    A failed write logs CRITICAL ``sequence_file_failed`` and stops the recorder through ``stop``
    (exit code 75, as the disk guard does), because a run without its holes file would hold back
    every later hour in the compactor. ``tripped`` tells ``main.py`` to exit 75.
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
        self.tripped = False

    @property
    def run(self) -> str:
        """The run folder the holes file is in, once started."""
        return "" if self._file is None else self._file.path.parent.name

    def start(self, now_ns: int) -> None:
        """Before the subscriptions: a fresh tracker, and the file's ``started_ns`` line."""
        self._tracker = SequenceTracker(confirm_ns=self._confirm_ns)
        self._file = HolesFile(self._path())
        self._write([{"started_ns": now_ns}])

    def observe(self, sequence: int, ts_init: int, now_ns: int) -> None:
        """One delta, in arrival order."""
        if self._tracker.observe(sequence, ts_init, now_ns):
            self._late += 1
            if self._late_first is None:
                self._late_first = sequence

    def tick(self, now_ns: int) -> None:
        """Confirm holes, log them, and write them with the new ``checked_to_ns``."""
        result = self._tracker.tick(now_ns)
        self._log_holes(result.holes)
        self._log_late()
        lines: list[Mapping[str, object]] = [h.to_dict() for h in result.holes]
        lines.append({"checked_to_ns": result.checked_to_ns})
        self._write(lines)

    def stop(self) -> None:
        """At stop, every open suspicion is a hole (fail safe), then ``closed``."""
        holes = self._tracker.close()
        self._log_holes(holes)
        self._log_late()
        lines: list[Mapping[str, object]] = [h.to_dict() for h in holes]
        lines.append({"closed": True})
        self._write(lines)

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

    def _write(self, lines: list[Mapping[str, object]]) -> None:
        if self.tripped or self._file is None:
            return
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
            self._stop()
