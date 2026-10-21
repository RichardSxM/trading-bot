"""Bar indicators for the scalping candidates (spec 0017 AC-4), on ``Decimal`` values only.

Each indicator is fed one bar at a time, only ever looks at what it was fed, and returns ``None``
until it is warm. The Wilder indicators (RSI, ATR, ADX) smooth with ``avg = (avg * (n - 1) + x) / n``
after a seed that is the simple mean of their first ``n`` inputs. A true range and a close to close
change both need a previous close, so RSI and ATR give their first value on the ``n + 1``-th bar, and
ADX (a Wilder average of ``n`` DX values) on the ``2n``-th.
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


def _check_period(name: str, n: int) -> None:
    if n < 1:
        raise ValueError(f"{name} needs n of at least 1, got {n}")


class Sma:
    """The simple mean of the last ``n`` inputs, ``None`` until ``n`` are in."""

    def __init__(self, n: int) -> None:
        _check_period("an SMA", n)
        self.n = n
        self.window: deque[Decimal] = deque(maxlen=n)

    def update(self, x: Decimal) -> Decimal | None:
        """Feed one input; return the mean after it."""
        self.window.append(x)
        if len(self.window) < self.n:
            return None
        return sum(self.window, _ZERO) / Decimal(self.n)


@dataclass(frozen=True)
class Bands:
    """One Bollinger reading: the middle (SMA) and the bands ``k`` population deviations away."""

    lower: Decimal
    middle: Decimal
    upper: Decimal


class Bollinger:
    """Bollinger bands over the last ``n`` closes: middle = SMA(n), bands = middle ± k population std devs."""

    def __init__(self, n: int, k: Decimal) -> None:
        if k <= 0:
            raise ValueError(f"Bollinger needs k above 0, got {k}")
        self._sma = Sma(n)
        self.k = k

    def update(self, close: Decimal) -> Bands | None:
        """Feed one close; return the bands after it."""
        middle = self._sma.update(close)
        if middle is None:
            return None
        window = self._sma.window
        variance = sum(((x - middle) ** 2 for x in window), _ZERO) / Decimal(len(window))
        width = self.k * variance.sqrt()
        return Bands(middle - width, middle, middle + width)


class _Wilder:
    """Wilder smoothing: seeded with the mean of the first ``n`` inputs, then ``(avg * (n - 1) + x) / n``."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.value: Decimal | None = None
        self._seed: list[Decimal] = []

    def update(self, x: Decimal) -> Decimal | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed, _ZERO) / Decimal(self.n)
                self._seed.clear()
            return self.value
        self.value = (self.value * Decimal(self.n - 1) + x) / Decimal(self.n)
        return self.value


class Rsi:
    """Wilder's RSI over ``n`` close to close changes; 100 when the average loss is 0."""

    def __init__(self, n: int) -> None:
        _check_period("an RSI", n)
        self._gain = _Wilder(n)
        self._loss = _Wilder(n)
        self._previous: Decimal | None = None

    def update(self, close: Decimal) -> Decimal | None:
        """Feed one close; return the RSI after it."""
        previous, self._previous = self._previous, close
        if previous is None:
            return None
        change = close - previous
        gain = self._gain.update(max(change, _ZERO))
        loss = self._loss.update(max(-change, _ZERO))
        if gain is None or loss is None:
            return None
        if loss == 0:
            return _HUNDRED
        return _HUNDRED - _HUNDRED / (1 + gain / loss)


def true_range(high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal:
    """The largest of high - low, |high - previous close|, and |low - previous close|."""
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


class Atr:
    """Wilder's average true range over ``n`` bars."""

    def __init__(self, n: int) -> None:
        _check_period("an ATR", n)
        self._average = _Wilder(n)
        self._previous: Decimal | None = None
        self.value: Decimal | None = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        """Feed one bar; return the ATR after it."""
        previous, self._previous = self._previous, close
        if previous is None:
            return None
        self.value = self._average.update(true_range(high, low, previous))
        return self.value


class Adx:
    """Wilder's ADX over ``n`` bars: +DI and -DI from the smoothed directional moves over the smoothed true
    range, DX = 100 * |+DI - -DI| / (+DI + -DI) (0 when both are 0), and ADX the Wilder average of DX."""

    def __init__(self, n: int) -> None:
        _check_period("an ADX", n)
        self._tr = _Wilder(n)
        self._plus = _Wilder(n)
        self._minus = _Wilder(n)
        self._dx = _Wilder(n)
        self._previous: tuple[Decimal, Decimal, Decimal] | None = None
        self.value: Decimal | None = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        """Feed one bar; return the ADX after it."""
        previous, self._previous = self._previous, (high, low, close)
        if previous is None:
            return None
        previous_high, previous_low, previous_close = previous
        up, down = high - previous_high, previous_low - low
        plus_dm = up if up > down and up > 0 else _ZERO
        minus_dm = down if down > up and down > 0 else _ZERO
        tr = self._tr.update(true_range(high, low, previous_close))
        plus = self._plus.update(plus_dm)
        minus = self._minus.update(minus_dm)
        if tr is None or plus is None or minus is None:
            return None
        if tr == 0:
            dx = _ZERO
        else:
            plus_di, minus_di = _HUNDRED * plus / tr, _HUNDRED * minus / tr
            total = plus_di + minus_di
            dx = _ZERO if total == 0 else _HUNDRED * abs(plus_di - minus_di) / total
        self.value = self._dx.update(dx)
        return self.value


class SessionVwap:
    """The volume weighted average typical price since the anchor: sum(typical * volume) / sum(volume).

    Typical = (high + low + close) / 3. ``anchor`` starts a new session; no value while the session's
    volume is 0.
    """

    def __init__(self) -> None:
        self.anchor_ns: int | None = None
        self._weighted = _ZERO
        self._volume = _ZERO
        self.bars = 0
        self.value: Decimal | None = None

    def anchor(self, ts_ns: int) -> None:
        """Start a new session at ``ts_ns``."""
        self.anchor_ns = ts_ns
        self._weighted = _ZERO
        self._volume = _ZERO
        self.bars = 0
        self.value = None

    def update(self, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> Decimal | None:
        """Feed one bar of the session; return the VWAP after it."""
        self._weighted += (high + low + close) / 3 * volume
        self._volume += volume
        self.bars += 1
        self.value = None if self._volume == 0 else self._weighted / self._volume
        return self.value


MINUTE_NS = 60_000_000_000


@dataclass(frozen=True)
class Delta:
    """One window's net aggressive volume (buy size - sell size) and its two sides."""

    delta: Decimal
    buy_size: Decimal
    sell_size: Decimal


class DeltaWindow:
    """The signed traded size of the last ``n`` closed 1 minute buckets.

    A trade adds its size to the bucket of its ``ts_event`` minute: + for a buyer aggressor, - for a
    seller, 0 for none. ``close(m)`` sums minutes ``m - n + 1`` to ``m`` (``None`` until ``n`` minutes
    were closed) and drops the buckets the next window no longer needs. A trade whose minute was already
    closed is dropped and counted in ``late_trades``.
    """

    def __init__(self, n: int) -> None:
        _check_period("a delta window", n)
        self.n = n
        self._buckets: dict[int, tuple[Decimal, Decimal]] = {}
        self._closed: int | None = None
        self._closes = 0
        self.late_trades = 0

    def add(self, ts_event_ns: int, size: Decimal, aggressor: str) -> None:
        """One trade; ``aggressor`` is ``buy``, ``sell``, or ``none``."""
        minute = ts_event_ns // MINUTE_NS
        if self._closed is not None and minute <= self._closed:
            self.late_trades += 1
            return
        buy, sell = self._buckets.get(minute, (_ZERO, _ZERO))
        if aggressor == "buy":
            buy += size
        elif aggressor == "sell":
            sell += size
        self._buckets[minute] = (buy, sell)

    def close(self, minute: int) -> Delta | None:
        """Close ``minute`` (minutes since the epoch); return the window ending at it."""
        self._closed = minute
        self._closes += 1
        first = minute - self.n + 1
        window = [self._buckets.get(m, (_ZERO, _ZERO)) for m in range(first, minute + 1)]
        # Keep what the next window (ending at minute + 1) still sums.
        self._buckets = {m: v for m, v in self._buckets.items() if m > first}
        if self._closes < self.n:
            return None
        buy = sum((b for b, _ in window), _ZERO)
        sell = sum((s for _, s in window), _ZERO)
        return Delta(buy - sell, buy, sell)
