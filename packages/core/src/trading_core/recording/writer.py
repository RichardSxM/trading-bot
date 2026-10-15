"""The off loop recording writer (spec 0006 AC-12, AC-13).

The node's event loop only ever calls ``RecordingWriter.put``, which never blocks. One worker
thread writes each item through the repository in order, one transaction per item:

- A transient error (connection or timeout) retries the same item after 1, 2, then every 5 s.
- A ``RecordConflictError`` is reported (``report_conflict``), dead lettered, and blocks entries
  with ``record_conflict`` until restart.
- Any other error is dead lettered, logged ``recording_failed``, and blocks entries with
  ``recording_failed`` until restart.

Entries are also blocked with ``recording_unhealthy`` while the oldest queued item is older
than the backlog limit or the newest write attempt failed; the block clears once the queue is
empty and the last write succeeded. A second thread checks this every second, so a write that
hangs (no error, no answer) still blocks entries once the backlog is too old. A full queue counts
as a failed write; its item goes to an overflow list, spilled with the rest on stop. Exits and
stops never look at any of this.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from enum import Enum, auto
from pathlib import Path
from typing import Final

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.errors import RecordConflictError
from trading_core.recording.spool import QueuedRow, WriteCall, WriteItem, append, read
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

RETRY_DELAYS_S: Final = (1.0, 2.0, 5.0)
"""Waits between attempts at a transient failure; the last one repeats."""

TRANSIENT_ERRORS: Final = (OperationalError, InterfaceError, PoolTimeoutError, ConnectionError, TimeoutError)
"""Errors that mean Postgres is unreachable or slow, not that the item is wrong."""

DEADLETTER_PREFIX: Final = "deadletter-"
HEALTH_CHECK_S: Final = 1.0
"""How often the health thread checks the backlog age, independent of the worker."""
_POLL_S: Final = 0.5

type Sessions = Callable[[], Session]
type WriteFn = Callable[[Session, QueuedRow], object]

CALLS: Final[Mapping[WriteCall, WriteFn]] = {
    WriteCall.UPSERT_INSTRUMENT: repository.upsert_instrument,  # type: ignore[dict-item]  # each takes its own row type
    WriteCall.RECORD_DECISION: repository.record_decision,  # type: ignore[dict-item]
    WriteCall.UPSERT_ORDER: repository.upsert_order,  # type: ignore[dict-item]
    WriteCall.RECORD_ORDER_EVENT: repository.record_order_event,  # type: ignore[dict-item]
    WriteCall.RECORD_FILL: repository.record_fill,  # type: ignore[dict-item]
    WriteCall.UPSERT_POSITION: repository.upsert_position,  # type: ignore[dict-item]
    WriteCall.RECORD_POSITION_SNAPSHOT: repository.record_position_snapshot,  # type: ignore[dict-item]
    WriteCall.RECORD_ACCOUNT_SNAPSHOT: repository.record_account_snapshot,  # type: ignore[dict-item]
    WriteCall.RECORD_RISK_EVENT: repository.record_risk_event,  # type: ignore[dict-item]
    WriteCall.RECORD_FUNDING: repository.record_funding,  # type: ignore[dict-item]
}
"""The repository function behind each call (``WriteItem`` guarantees the row type matches)."""


class _Outcome(Enum):
    WRITTEN = auto()
    TRANSIENT = auto()
    FAILED = auto()


def spool_path(spool_dir: Path, run_id: object) -> Path:
    """Where a stop spills the items it could not write: ``<spool_dir>/<run_id>.jsonl``."""
    return spool_dir / f"{run_id}.jsonl"


def deadletter_path(spool_dir: Path, run_id: object) -> Path:
    """Where failed items go, never replayed: ``<spool_dir>/deadletter-<run_id>.jsonl``."""
    return spool_dir / f"{DEADLETTER_PREFIX}{run_id}.jsonl"


def write_one(sessions: Sessions, item: WriteItem, deadletter: Path, blocks: EntryBlocks) -> _Outcome:
    """Write ``item`` in its own transaction; on a conflict or a bad item, dead letter it and block entries."""
    session = sessions()
    try:
        with session.begin():
            CALLS[item.call](session, item.row)
        return _Outcome.WRITTEN
    except RecordConflictError as exc:
        _report(sessions, exc)
        append(deadletter, [item])
        blocks.add(BlockReason.RECORD_CONFLICT)
        log.error(
            "record contradicts the database, entries blocked until restart",
            extra={"fields": {"code": "record_conflict", "call": item.call.value, "table": exc.table}},
        )
        return _Outcome.FAILED
    except TRANSIENT_ERRORS:
        return _Outcome.TRANSIENT
    except Exception:
        append(deadletter, [item])
        blocks.add(BlockReason.RECORDING_FAILED)
        log.exception(
            "record could not be written, entries blocked until restart",
            extra={"fields": {"code": "recording_failed", "call": item.call.value}},
        )
        return _Outcome.FAILED
    finally:
        session.close()


def _report(sessions: Sessions, error: RecordConflictError) -> None:
    try:
        with sessions() as session:
            repository.report_conflict(session, error)
    except Exception:
        log.exception("record conflict could not be reported", extra={"fields": {"code": "record_conflict_unreported"}})


class RecordingWriter:
    """A bounded queue and one worker thread writing each item through the repository, in order."""

    def __init__(
        self,
        sessions: Sessions,
        blocks: EntryBlocks,
        *,
        queue_max: int,
        max_backlog_s: float,
        deadletter: Path,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = sessions
        self._blocks = blocks
        self._queue: queue.Queue[tuple[float, WriteItem]] = queue.Queue(maxsize=queue_max)
        self._max_backlog_s = max_backlog_s
        self._deadletter = deadletter
        self._delays = tuple(retry_delays_s)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._overflow: list[WriteItem] = []
        self._current: tuple[float, WriteItem] | None = None
        self._last_ok = True
        self._closing = threading.Event()
        self._abort = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recording-writer", daemon=True)
        self._health = threading.Thread(target=self._watch, name="recording-health", daemon=True)

    def start(self) -> None:
        """Start the worker thread and the health thread."""
        self._thread.start()
        self._health.start()

    def put(self, item: WriteItem) -> None:
        """Queue ``item``; never blocks. A full queue counts as a failed write and keeps the item in memory."""
        try:
            self._queue.put_nowait((self._monotonic(), item))
        except queue.Full:
            with self._lock:
                self._overflow.append(item)
                self._last_ok = False
            self._evaluate()

    @property
    def backlog(self) -> int:
        """Items not yet written: queued, being written, and overflowed."""
        with self._lock:
            return self._queue.qsize() + len(self._overflow) + (self._current is not None)

    @property
    def last_ok(self) -> bool:
        """False when the newest write attempt failed (or the queue was full)."""
        with self._lock:
            return self._last_ok

    def oldest_age_s(self) -> float:
        """Seconds the oldest unwritten queued item has waited; 0 when none waits."""
        with self._lock:
            current = self._current
        oldest = current[0] if current is not None else self._peek()
        return 0.0 if oldest is None else self._monotonic() - oldest

    def drain(self, timeout_s: float) -> bool:
        """Stop taking work once the queue is empty, waiting up to ``timeout_s``; True when all was written."""
        self._closing.set()
        if self._thread.is_alive():
            self._thread.join(timeout_s)
        self._abort.set()
        with self._lock:
            return not self._thread.is_alive() and self._queue.empty() and not self._overflow and self._current is None

    def pending(self) -> list[WriteItem]:
        """Every item not yet written, oldest first: the one in flight, the queue, then the overflow."""
        items: list[WriteItem] = []
        with self._lock:
            if self._current is not None:
                items.append(self._current[1])
            with self._queue.mutex:
                items.extend(item for _, item in self._queue.queue)
            items.extend(self._overflow)
        return items

    def spill(self, path: Path) -> int:
        """Append every unwritten item to ``path`` (call after ``drain``); returns how many."""
        items = self.pending()
        append(path, items)
        return len(items)

    def _peek(self) -> float | None:
        with self._queue.mutex:
            return self._queue.queue[0][0] if self._queue.queue else None

    def _run(self) -> None:
        while not self._abort.is_set():
            try:
                entry = self._queue.get(timeout=_POLL_S)
            except queue.Empty:
                self._evaluate()
                if self._closing.is_set():
                    return
                continue
            with self._lock:
                self._current = entry
            if self._write(entry[1]):
                with self._lock:
                    self._current = None
            self._evaluate()

    def _watch(self) -> None:
        while not self._abort.wait(HEALTH_CHECK_S):
            self._evaluate()

    def _write(self, item: WriteItem) -> bool:
        """Write until done or dead lettered (True), or until aborted mid retry (False, the item stays pending)."""
        attempt = 0
        while True:
            outcome = write_one(self._sessions, item, self._deadletter, self._blocks)
            with self._lock:
                self._last_ok = outcome is _Outcome.WRITTEN
            if outcome is not _Outcome.TRANSIENT:
                return True
            self._evaluate()
            delay = self._delays[min(attempt, len(self._delays) - 1)]
            attempt += 1
            if self._abort.wait(delay):
                return False

    def _evaluate(self) -> None:
        with self._lock:
            last_ok, idle = self._last_ok, self._current is None
        unhealthy = not last_ok or self.oldest_age_s() > self._max_backlog_s
        if unhealthy:
            if self._blocks.add(BlockReason.RECORDING_UNHEALTHY):
                log.error(
                    "recording is behind or failing, entries blocked",
                    extra={"fields": {"code": "recording_unhealthy", "backlog": self.backlog}},
                )
        elif idle and self._queue.empty() and self._blocks.clear(BlockReason.RECORDING_UNHEALTHY):
            log.info("recording healthy again", extra={"fields": {"code": "recording_healthy"}})


def replay_spool(spool_dir: Path, sessions: Sessions, blocks: EntryBlocks) -> int:
    """Write every spill file in ``spool_dir`` through the repository, then delete it; returns items replayed.

    Runs at start, before trading. Dead letter files are never replayed. A conflicting or bad
    item is dead lettered under the run that spilled it and blocks entries like a live write.
    A transient error raises, so the start fails and the file is kept for the next try.
    """
    if not spool_dir.is_dir():
        return 0
    replayed = 0
    for path in sorted(spool_dir.glob("*.jsonl")):
        if path.name.startswith(DEADLETTER_PREFIX):
            continue
        deadletter = deadletter_path(spool_dir, path.stem)
        for item in read(path):
            if write_one(sessions, item, deadletter, blocks) is _Outcome.TRANSIENT:
                raise ConnectionError(f"Postgres unavailable while replaying {path.name}")
            replayed += 1
        path.unlink()
        log.info("spool replayed", extra={"fields": {"code": "spool_replayed", "file": path.name}})
    return replayed
