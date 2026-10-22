"""Bar indicators for the hour level candidates (spec 0018 AC-6): SMA, Wilder RSI, Wilder ATR, Bollinger bands.

All are ``Decimal`` in and out, fed one bar at a time, and only ever look at the values fed so far, so
a rule built on them cannot read ahead of its bar (spec 0014 AC-19). Each returns ``None`` until it is
ready: ``Sma`` and ``Bollinger`` from input ``n``, ``Rsi`` and ``Atr`` from input ``n + 1`` (their first
input has no change or true range). ``trading_core.strategy.indicators`` (EMA, MACD) is a separate
module and stays unchanged, so the benchmarks' code hashes don't move.
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


def _check_n(name: str, n: int, least: int) -> None:
    if n < least:
        raise ValueError(f"{name} needs n of at least {least}, got {n}")


class Sma:
    """A simple moving average of the last ``n`` inputs, ``None`` until ``n`` inputs are in."""

    def __init__(self, n: int) -> None:
        _check_n("an SMA", n, 1)
        self.n = n
        self.value: Decimal | None = None
        self._window: deque[Decimal] = deque(maxlen=n)

    def update(self, x: Decimal) -> Decimal | None:
        """Feed one input; return the average after it."""
        self._window.append(x)
        if len(self._window) == self.n:
            self.value = sum(self._window, _ZERO) / Decimal(self.n)
        return self.value


class Rsi:
    """Wilder's RSI over ``n`` changes of the input.

    The average gain and loss are seeded with the simple mean of the first ``n`` changes, then move
    as ``(previous * (n - 1) + x) ÷ n``. RSI is 100 when the average loss is 0 and the gain is not,
    and 50 when both are 0.
    """

    def __init__(self, n: int) -> None:
        _check_n("an RSI", n, 1)
        self.n = n
        self.value: Decimal | None = None
        self._last: Decimal | None = None
        self._gains: list[Decimal] = []
        self._losses: list[Decimal] = []
        self._avg_gain: Decimal | None = None
        self._avg_loss = _ZERO

    def update(self, x: Decimal) -> Decimal | None:
        """Feed one input; return the RSI after it."""
        last, self._last = self._last, x
        if last is None:
            return None
        change = x - last
        gain, loss = max(change, _ZERO), max(-change, _ZERO)
        n = Decimal(self.n)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.n:
                return None
            self._avg_gain = sum(self._gains, _ZERO) / n
            self._avg_loss = sum(self._losses, _ZERO) / n
            self._gains.clear()
            self._losses.clear()
        else:
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        self.value = _rsi(self._avg_gain, self._avg_loss)
        return self.value


def _rsi(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal(50) if avg_gain == 0 else _HUNDRED
    return _HUNDRED - _HUNDRED / (1 + avg_gain / avg_loss)


class Atr:
    """Wilder's average true range over ``n`` bars.

    The true range is the largest of high - low, |high - previous close|, and |low - previous close|;
    the first bar has none. The average is seeded with the mean of the first ``n`` true ranges, then
    moves as ``(previous * (n - 1) + tr) ÷ n``.
    """

    def __init__(self, n: int) -> None:
        _check_n("an ATR", n, 1)
        self.n = n
        self.value: Decimal | None = None
        self._close: Decimal | None = None
        self._seed: list[Decimal] = []

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        """Feed one bar; return the ATR after it."""
        previous, self._close = self._close, close
        if previous is None:
            return None
        tr = max(high - low, abs(high - previous), abs(low - previous))
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.n:
                self.value = sum(self._seed, _ZERO) / Decimal(self.n)
                self._seed.clear()
            return self.value
        self.value = (self.value * (self.n - 1) + tr) / Decimal(self.n)
        return self.value


@dataclass(frozen=True)
class Bands:
    """One Bollinger reading: the middle (the SMA) and the bands ``k`` standard deviations around it."""

    middle: Decimal
    upper: Decimal
    lower: Decimal


class Bollinger:
    """Bollinger bands: SMA(n) ± k * the population standard deviation of the last ``n`` closes."""

    def __init__(self, n: int, k: Decimal) -> None:
        _check_n("Bollinger bands", n, 1)
        if k <= 0:
            raise ValueError(f"Bollinger bands need k above 0, got {k}")
        self.n = n
        self.k = k
        self.value: Bands | None = None
        self._window: deque[Decimal] = deque(maxlen=n)

    def update(self, close: Decimal) -> Bands | None:
        """Feed one close; return the bands after it."""
        self._window.append(close)
        if len(self._window) < self.n:
            return None
        n = Decimal(self.n)
        middle = sum(self._window, _ZERO) / n
        variance = sum(((x - middle) ** 2 for x in self._window), _ZERO) / n
        width = self.k * variance.sqrt()
        self.value = Bands(middle, middle + width, middle - width)
        return self.value
