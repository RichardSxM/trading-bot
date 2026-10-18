"""Swing highs and lows, confirmed only by the bars after them (spec 0014 AC-20).

A swing high at bar ``i`` is a bar whose high is strictly above the highs of the ``left`` bars before
it and the ``right`` bars after it; a swing low mirrors it with lows. It cannot be known until bar
``i + right`` has closed, so ``SwingTracker.update`` returns it only from the call for that bar, never
earlier. A signal built on a swing therefore fires on the confirming bar, which keeps a backtest
honest (the scope's look ahead note for feature 13).
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class SwingKind(StrEnum):
    """Whether a swing is a high or a low."""

    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Swing:
    """One confirmed swing: the bar it sits on, its price, and the bar that confirmed it."""

    kind: SwingKind
    bar_index: int
    price: Decimal
    confirmed_index: int


class SwingOrderError(ValueError):
    """Bars were fed to a ``SwingTracker`` out of order (each index must follow the last by one)."""


class SwingTracker:
    """Feeds bars in order and returns each swing on the bar that confirms it."""

    def __init__(self, left: int, right: int) -> None:
        if left < 1 or right < 1:
            raise ValueError(f"left and right must be at least 1, got {left} and {right}")
        self.left = left
        self.right = right
        self._bars: deque[tuple[int, Decimal, Decimal]] = deque(maxlen=left + right + 1)

    def update(self, index: int, high: Decimal, low: Decimal) -> list[Swing]:
        """Feed bar ``index``; return the swings it confirms (at bar ``index - right``), if any."""
        if self._bars and index != self._bars[-1][0] + 1:
            raise SwingOrderError(f"bar {index} does not follow bar {self._bars[-1][0]}")
        self._bars.append((index, high, low))
        if len(self._bars) < self.left + self.right + 1:
            return []
        bars = list(self._bars)
        at, mid_high, mid_low = bars[self.left]
        others = bars[: self.left] + bars[self.left + 1 :]
        found: list[Swing] = []
        if all(mid_high > h for _, h, _ in others):
            found.append(Swing(SwingKind.HIGH, at, mid_high, index))
        if all(mid_low < lo for _, _, lo in others):
            found.append(Swing(SwingKind.LOW, at, mid_low, index))
        return found
