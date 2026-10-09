"""Markouts (spec 0008 AC-10): how far the mid moved for or against a fill, h seconds after it.

The markout at horizon h is ``side * (mid(fill_ns + h) - fill_px) / fill_px * 10,000`` in basis
points, where ``side`` is +1 for a buy and ``mid(t)`` is the last mid at or before t in the same
segment. A fill whose ``fill_ns + h`` lies after the segment's last delta has no markout at h
(it is left out of that horizon's mean).

``MarkoutTracker`` works as the replay goes: it keeps only the fills whose horizons are still
pending, never a history of mids, so its memory stays small over a whole day (AC-16).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

HORIZONS_S: Final = (1, 5, 30)
NS_PER_S: Final = 1_000_000_000
BPS: Final = Decimal(10_000)


def markout_bps(buy: bool, fill_px: Decimal, mid: Decimal) -> Decimal:
    """One fill's markout in basis points: positive when the mid moved in the fill's favour."""
    if fill_px <= 0:
        raise ValueError(f"fill price must be positive, got {fill_px}")
    side = 1 if buy else -1
    return side * (mid - fill_px) / fill_px * BPS


@dataclass(frozen=True)
class FillMarkouts:
    """A fill's markout at each horizon (seconds), None when the horizon passed the segment's end."""

    trade_id: str
    markouts: Mapping[int, Decimal | None]


@dataclass
class _Pending:
    trade_id: str
    buy: bool
    fill_px: Decimal
    fill_ns: int
    found: dict[int, Decimal | None] = field(default_factory=dict)


class MarkoutTracker:
    """Collects the markouts of a segment's fills from its mids, in time order.

    Call ``on_mid`` for every mid change and ``on_fill`` for every fill, both in the order the
    replay sees them, then ``finish`` with the segment's last delta time.
    """

    def __init__(self, horizons_s: tuple[int, ...] = HORIZONS_S) -> None:
        self._horizons = tuple(sorted(horizons_s))
        self._mid: Decimal | None = None
        self._pending: list[_Pending] = []
        self._done: list[FillMarkouts] = []

    @property
    def waiting(self) -> bool:
        """True while some fill still waits for a horizon's mid."""
        return bool(self._pending)

    def on_fill(self, trade_id: str, *, buy: bool, fill_px: Decimal, fill_ns: int) -> None:
        """A fill to mark out."""
        self._pending.append(_Pending(trade_id, buy, fill_px, fill_ns))

    def on_mid(self, ts_ns: int, mid: Decimal) -> None:
        """The mid became ``mid`` at ``ts_ns``. Horizons that ended before ``ts_ns`` take the previous mid."""
        if self._pending:
            self._resolve(lambda target: target < ts_ns)
            self._mid = mid
            self._resolve(lambda target: target == ts_ns)
        else:
            self._mid = mid

    def finish(self, last_ts_ns: int) -> list[FillMarkouts]:
        """Close the segment: horizons up to its last delta take the last mid, later ones are left out."""
        self._resolve(lambda target: target <= last_ts_ns)
        for pending in self._pending:
            for h in self._horizons:
                pending.found.setdefault(h, None)
            self._done.append(FillMarkouts(pending.trade_id, dict(pending.found)))
        self._pending = []
        return self._done

    def _resolve(self, due: Callable[[int], bool]) -> None:
        if self._mid is None:
            return
        still: list[_Pending] = []
        for pending in self._pending:
            for h in self._horizons:
                if h not in pending.found and due(pending.fill_ns + h * NS_PER_S):
                    pending.found[h] = markout_bps(pending.buy, pending.fill_px, self._mid)
            if len(pending.found) == len(self._horizons):
                self._done.append(FillMarkouts(pending.trade_id, dict(pending.found)))
            else:
                still.append(pending)
        self._pending = still
