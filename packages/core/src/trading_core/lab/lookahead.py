"""The look ahead check's pure rules (spec 0014 AC-19): where to cut, and how a cut run must match.

A strategy that only sees what the engine has fed decides the same before a cut whether or not the
data after it exists. So the full run is run again on the same data cut at a few times, and every
decision strictly before a cut must be one of the cut run's, with no extra one there. The lab
leaves out the ``range_end`` decisions before it calls ``first_mismatch``.

A cut never falls on a bar close. It is a whole bar interval plus 1 minute to one minute short of
the next close (for hour bars 1 to 59 minutes past the hour, for 15 minute bars 1 to 14 minutes
past a quarter hour, spec 0019 AC-2), so the cut run has the bar that closed before the cut but
not the one that closes after it. A strategy that decides about a bar from the next one stamps
that decision before the cut while the cut run never sees the next bar, so even a one bar peek
is caught.

Times are Nautilus nanosecond ints (UTC).
"""

import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from trading_core.lab.candles import MINUTE_NS
from trading_core.marketdata.hours import HOUR_NS

# The share of the range at each end where no cut is drawn.
EDGE_SHARE_PCT: Final = 10
# A cut lies at least this many whole minutes after a bar close: never on the close itself.
CUT_FIRST_MINUTE: Final = 1


def cut_times(seed: int, start_ns: int, end_ns: int, count: int, interval_ns: int = HOUR_NS) -> list[int]:
    """``count`` cuts in the range's middle 80 %, sorted: each strictly between two ``interval_ns`` bar closes.

    A cut is a distinct whole interval plus 1 minute up to one minute short of the next close.
    ``random.Random(seed)`` first samples the intervals, from those whose last cut minute still
    lies in the middle, then draws each cut's minute; with the default hour interval that is a
    whole hour plus 1 to 59 minutes. Raises ``ValueError`` when the interval is not a whole number
    of at least 2 minutes, or when fewer than ``count`` intervals qualify.
    """
    if interval_ns % MINUTE_NS or interval_ns < 2 * MINUTE_NS:
        raise ValueError(f"a cut needs bars of whole minutes, at least 2, not {interval_ns} ns")
    last_minute = interval_ns // MINUTE_NS - 1
    edge = (end_ns - start_ns) * EDGE_SHARE_PCT // 100
    low, high = start_ns + edge, end_ns - edge
    first = -(-low // interval_ns) * interval_ns
    closes = range(first, high - last_minute * MINUTE_NS + 1, interval_ns)
    if len(closes) < count:
        raise ValueError(f"the range's middle holds {len(closes)} whole bar intervals, fewer than the {count} cuts")
    rng = random.Random(seed)
    drawn = rng.sample(closes, count)
    return sorted(close + rng.randint(CUT_FIRST_MINUTE, last_minute) * MINUTE_NS for close in drawn)


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
