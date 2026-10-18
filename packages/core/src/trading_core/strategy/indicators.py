"""Bar indicators on ``Decimal`` closes (spec 0014 AC-13): an EMA and MACD.

Each indicator is fed one close at a time and only ever looks at the values fed so far, so a strategy
built on them cannot read ahead of the bar it is on (AC-19). An EMA uses alpha = 2 / (n + 1) and is seeded
with the simple mean of its first ``n`` inputs; it has no value before that.
"""

from dataclasses import dataclass
from decimal import Decimal


class Ema:
    """An exponential moving average over ``n`` inputs, ``None`` until ``n`` inputs are in."""

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"an EMA needs n of at least 1, got {n}")
        self.n = n
        self.value: Decimal | None = None
        self._alpha = Decimal(2) / Decimal(n + 1)
        self._seed: list[Decimal] = []

    def update(self, x: Decimal) -> Decimal | None:
        """Feed one input; return the EMA after it (``None`` while still seeding)."""
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed, Decimal(0)) / Decimal(self.n)
                self._seed.clear()
            return self.value
        self.value = self.value + self._alpha * (x - self.value)
        return self.value


@dataclass(frozen=True)
class MacdPoint:
    """One MACD reading: the line (fast EMA - slow EMA), its signal EMA, and the histogram."""

    macd: Decimal
    signal: Decimal
    histogram: Decimal


class Macd:
    """MACD on closes: the fast and slow EMAs, and a signal EMA of their difference.

    The signal EMA starts on the first MACD line value, so the first histogram arrives on input
    ``slow + signal - 1``.
    """

    def __init__(self, fast: int, slow: int, signal: int) -> None:
        if fast >= slow:
            raise ValueError(f"MACD needs fast below slow, got fast {fast} and slow {slow}")
        self._fast = Ema(fast)
        self._slow = Ema(slow)
        self._signal = Ema(signal)

    def update(self, close: Decimal) -> MacdPoint | None:
        """Feed one close; return the reading after it, ``None`` until the histogram has a value."""
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None
        line = fast - slow
        signal = self._signal.update(line)
        if signal is None:
            return None
        return MacdPoint(line, signal, line - signal)
