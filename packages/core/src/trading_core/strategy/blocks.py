"""Why the trader is not taking new entries (spec 0006, the *Block reasons* table).

``EntryBlocks`` is a thread safe set shared by the strategy, its timers, and the recording
writer thread. Exits and stops are never blocked; only entries look at it.
"""

import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Final


class BlockReason(StrEnum):
    """One reason entries are blocked. ``halted`` and ``paused`` come from the gate, not from ``EntryBlocks``."""

    HALTED = "halted"
    PAUSED = "paused"
    STALE_FEED = "stale_feed"
    RECORDING_UNHEALTHY = "recording_unhealthy"
    STOP_NOT_ACCEPTED = "stop_not_accepted"
    EXIT_FAILED = "exit_failed"
    RECORD_CONFLICT = "record_conflict"
    RECORDING_FAILED = "recording_failed"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    TRADE_CAP = "trade_cap"
    LOSS_COOLDOWN = "loss_cooldown"
    LOSS_UNKNOWN = "loss_unknown"
    REGIME = "regime"


PLAIN_WORDS: Final[dict[BlockReason, str]] = {
    BlockReason.HALTED: "Halted by the stop button or a loss limit (press Resume to clear)",
    BlockReason.PAUSED: "Paused from the dashboard or by a deploy",
    BlockReason.STALE_FEED: "No fresh prices from Kraken",
    BlockReason.RECORDING_UNHEALTHY: "The database is behind or not answering",
    BlockReason.STOP_NOT_ACCEPTED: "A stop was not accepted, so the position was closed (restart to clear)",
    BlockReason.EXIT_FAILED: "Closing a position failed 5 times (restart to clear)",
    BlockReason.RECORD_CONFLICT: "A record contradicted the database (restart to clear)",
    BlockReason.RECORDING_FAILED: "A record could not be written (restart to clear)",
    BlockReason.DAILY_LOSS_LIMIT: "Today's loss reached the daily limit (clears at midnight UTC)",
    BlockReason.TRADE_CAP: "Today's trade cap is reached (clears at midnight UTC)",
    BlockReason.LOSS_COOLDOWN: "Cooling down after a losing trade",
    BlockReason.LOSS_UNKNOWN: "Today's loss is not known yet (waiting for the database)",
    BlockReason.REGIME: "The market is trending (ADX above the limit), entries wait",
}
"""What the dashboard shows for each reason."""

SELF_CLEARING: Final = frozenset(
    {
        BlockReason.PAUSED,
        BlockReason.STALE_FEED,
        BlockReason.RECORDING_UNHEALTHY,
        BlockReason.DAILY_LOSS_LIMIT,
        BlockReason.TRADE_CAP,
        BlockReason.LOSS_COOLDOWN,
        BlockReason.LOSS_UNKNOWN,
        BlockReason.REGIME,
    }
)
"""Reasons that clear by themselves; the rest hold until the trader restarts."""


class EntryBlocks:
    """The active block reasons, safe to change from any thread, with when each became active.

    ``now_ns`` gives the UTC time in nanoseconds (the wall clock unless a test passes its own).
    """

    def __init__(self, now_ns: Callable[[], int] = time.time_ns) -> None:
        self._lock = threading.Lock()
        self._active: set[BlockReason] = set()
        self._since: dict[BlockReason, int] = {}
        self._now_ns = now_ns

    def add(self, reason: BlockReason) -> bool:
        """Block entries for ``reason``; True when it was not already active."""
        with self._lock:
            if reason in self._active:
                return False
            self._active.add(reason)
            self._since[reason] = self._now_ns()
            return True

    def clear(self, reason: BlockReason) -> bool:
        """Lift ``reason``; True when it was active. Only a self clearing reason may be lifted."""
        if reason not in SELF_CLEARING:
            raise ValueError(f"{reason} holds until restart")
        with self._lock:
            if reason not in self._active:
                return False
            self._active.discard(reason)
            self._since.pop(reason, None)
            return True

    def active(self) -> tuple[BlockReason, ...]:
        """The active reasons, sorted."""
        with self._lock:
            return tuple(sorted(self._active))

    def since(self) -> dict[str, int]:
        """The ``entries_blocked_since`` snapshot field (spec 0013 AC-6): each active reason to when
        it became active, in UTC nanoseconds."""
        with self._lock:
            return {str(reason): ns for reason, ns in sorted(self._since.items())}


def entries_blocked(blocks: EntryBlocks, paused: bool, halted: bool) -> list[str]:
    """The ``entries_blocked`` snapshot field: the active reasons plus ``paused`` and ``halted`` when set, sorted."""
    reasons = {str(r) for r in blocks.active()}
    if paused:
        reasons.add(BlockReason.PAUSED)
    if halted:
        reasons.add(BlockReason.HALTED)
    return sorted(reasons)
