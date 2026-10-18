"""The look ahead check's pure rules (spec 0014 AC-19): where to cut, and how a cut run must match.

A strategy that only sees what the engine has fed decides the same before a cut whether or not the
data after it exists. So the full run is run again on the same data cut at a few whole hours, and
every decision strictly before a cut must be one of the cut run's, with no extra one there. The
lab leaves out the ``range_end`` decisions before it calls ``first_mismatch``.

Times are Nautilus nanosecond ints (UTC).
"""

import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from trading_core.marketdata.hours import HOUR_NS

# The share of the range at each end where no cut is drawn.
EDGE_SHARE_PCT: Final = 10


def cut_times(seed: int, start_ns: int, end_ns: int, count: int) -> list[int]:
    """``count`` distinct whole hours drawn with ``random.Random(seed)`` from the range's middle 80 %, sorted.

    Raises ``ValueError`` when the middle of the range holds fewer than ``count`` whole hours.
    """
    edge = (end_ns - start_ns) * EDGE_SHARE_PCT // 100
    low, high = start_ns + edge, end_ns - edge
    first = -(-low // HOUR_NS) * HOUR_NS
    hours = list(range(first, high + 1, HOUR_NS))
    if len(hours) < count:
        raise ValueError(f"the range's middle holds {len(hours)} whole hours, fewer than the {count} cuts")
    return sorted(random.Random(seed).sample(hours, count))


@dataclass(frozen=True)
class DecidedKey:
    """What a decision must keep across a cut: its time, intent, side, reason, and risk verdict."""

    decided_ns: int
    intent: str
    side: str | None
    reason: str | None
    verdict: str | None

    def order(self) -> tuple[int, str, str, str, str]:
        """A total order, with None sorting first."""
        return self.decided_ns, self.intent, self.side or "", self.reason or "", self.verdict or ""


@dataclass(frozen=True)
class LookaheadMismatch:
    """The first decision before the cut where the two runs differ; a side is None when that run has none there."""

    cut_ns: int
    full: DecidedKey | None
    cut: DecidedKey | None


def first_mismatch(full: Iterable[DecidedKey], cut: Iterable[DecidedKey], cut_ns: int) -> LookaheadMismatch | None:
    """Compare the decisions with ``decided_ns < cut_ns`` of both runs in time order; None when they are equal."""
    before_full = sorted((k for k in full if k.decided_ns < cut_ns), key=DecidedKey.order)
    before_cut = sorted((k for k in cut if k.decided_ns < cut_ns), key=DecidedKey.order)
    for i in range(max(len(before_full), len(before_cut))):
        a = before_full[i] if i < len(before_full) else None
        b = before_cut[i] if i < len(before_cut) else None
        if a != b:
            return LookaheadMismatch(cut_ns, a, b)
    return None
