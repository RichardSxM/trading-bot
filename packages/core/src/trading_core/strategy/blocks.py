"""Why the trader is not taking new entries (spec 0006, the *Block reasons* table).

``EntryBlocks`` is a thread safe set shared by the strategy, its timers, and the recording
writer thread. Exits and stops are never blocked; only entries look at it.
"""

import threading
from enum import StrEnum
from typing import Final


class BlockReason(StrEnum):
    """One reason entries are blocked. ``paused`` comes from the pause gate, not from ``EntryBlocks``."""

    PAUSED = "paused"
    STALE_FEED = "stale_feed"
    RECORDING_UNHEALTHY = "recording_unhealthy"
    STOP_NOT_ACCEPTED = "stop_not_accepted"
    EXIT_FAILED = "exit_failed"
    RECORD_CONFLICT = "record_conflict"
    RECORDING_FAILED = "recording_failed"


PLAIN_WORDS: Final[dict[BlockReason, str]] = {
    BlockReason.PAUSED: "Paused from the dashboard or by a deploy",
    BlockReason.STALE_FEED: "No fresh prices from Kraken",
    BlockReason.RECORDING_UNHEALTHY: "The database is behind or not answering",
    BlockReason.STOP_NOT_ACCEPTED: "A stop was not accepted, so the position was closed (restart to clear)",
    BlockReason.EXIT_FAILED: "Closing a position failed 5 times (restart to clear)",
    BlockReason.RECORD_CONFLICT: "A record contradicted the database (restart to clear)",
    BlockReason.RECORDING_FAILED: "A record could not be written (restart to clear)",
}
"""What the dashboard shows for each reason."""

SELF_CLEARING: Final = frozenset({BlockReason.PAUSED, BlockReason.STALE_FEED, BlockReason.RECORDING_UNHEALTHY})
"""Reasons that clear by themselves; the rest hold until the trader restarts."""


class EntryBlocks:
    """The active block reasons, safe to change from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[BlockReason] = set()

    def add(self, reason: BlockReason) -> bool:
        """Block entries for ``reason``; True when it was not already active."""
        with self._lock:
            if reason in self._active:
                return False
            self._active.add(reason)
            return True

    def clear(self, reason: BlockReason) -> bool:
        """Lift ``reason``; True when it was active. Only a self clearing reason may be lifted."""
        if reason not in SELF_CLEARING:
            raise ValueError(f"{reason} holds until restart")
        with self._lock:
            if reason not in self._active:
                return False
            self._active.discard(reason)
            return True

    def active(self) -> tuple[BlockReason, ...]:
        """The active reasons, sorted."""
        with self._lock:
            return tuple(sorted(self._active))


def entries_blocked(blocks: EntryBlocks, paused: bool) -> list[str]:
    """The ``entries_blocked`` snapshot field: the active reasons plus ``paused`` when paused, sorted."""
    reasons = {str(r) for r in blocks.active()}
    if paused:
        reasons.add(BlockReason.PAUSED)
    return sorted(reasons)
