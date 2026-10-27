"""The noise area helpers of ``noise_breakout`` (spec 0020, *Noise helpers*, AC-3): the day's VWAP and bands.

Both key every bar by the UTC date of its **open** time (``bar_open_ns``), so the bar from 23:30 to 00:00
belongs to the old day and the bar from 00:00 to 00:30 to the new one. Both only ever look at the bars fed
so far, so a rule built on them cannot read ahead of its bar (spec 0014 AC-19). All ``Decimal``, in the
default context, with no rounding. Frozen after the first study that has it in its closure (spec 0018
AC-7): a change goes into a new module.
"""

from collections import deque
from datetime import UTC, date, datetime
from decimal import Decimal

DAY_NS = 86_400_000_000_000
_ZERO = Decimal(0)
_THREE = Decimal(3)


def utc_date(ts_ns: int) -> date:
    """The UTC calendar date of a nanosecond timestamp."""
    return datetime.fromtimestamp(ts_ns // DAY_NS * 86_400, UTC).date()


class DayVwap:
    """The volume weighted average typical price of the UTC day so far, reset at each UTC midnight.

    Typical = (high + low + close) / 3. ``None`` while the day's volume is 0.
    """

    def __init__(self) -> None:
        self.day: date | None = None
        self._cum_pv = _ZERO
        self._cum_v = _ZERO

    def update(self, bar_open_ns: int, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> Decimal | None:
        """Feed one bar (keyed by its open time) and return the day's VWAP after it."""
        day = utc_date(bar_open_ns)
        if day != self.day:
            self.day, self._cum_pv, self._cum_v = day, _ZERO, _ZERO
        self._cum_pv += (high + low + close) / _THREE * volume
        self._cum_v += volume
        return None if self._cum_v == 0 else self._cum_pv / self._cum_v


class NoiseBands:
    """Each time of day slot's noise ``sigma``: the mean move from the day open over earlier days.

    A slot is the bar's place in its UTC day, ``(bar_open_ns - midnight) // interval_ns`` (0 to 47 for 30
    minute bars). ``day_open`` is the open of the day's slot 0 bar, or ``None`` for the whole day when its
    first bar is not slot 0 (fail closed: no bands that day). A bar's ``sigma`` is the mean of its slot's
    history once that holds exactly ``lookback_days`` values, read before this bar's own move
    ``|close / day_open - 1|`` is added, so it uses earlier days only. A day missing the slot, or with no
    ``day_open``, adds nothing, so the history is the slot's most recent ``lookback_days`` days that had it.
    """

    def __init__(self, lookback_days: int, interval_ns: int) -> None:
        if lookback_days < 1:
            raise ValueError(f"lookback_days must be at least 1, got {lookback_days}")
        if interval_ns < 1 or DAY_NS % interval_ns:
            raise ValueError(f"interval_ns must divide one day, got {interval_ns}")
        self.lookback_days = lookback_days
        self.interval_ns = interval_ns
        self.day: date | None = None
        self.slot: int | None = None
        self.day_open: Decimal | None = None
        self._history: dict[int, deque[Decimal]] = {}

    def update(self, bar_open_ns: int, open: Decimal, close: Decimal) -> Decimal | None:
        """Feed one bar (keyed by its open time) and return its slot's ``sigma``, or ``None``."""
        day = utc_date(bar_open_ns)
        slot = bar_open_ns % DAY_NS // self.interval_ns
        if day != self.day:
            self.day, self.day_open = day, open if slot == 0 else None
        self.slot = slot
        history = self._history.setdefault(slot, deque(maxlen=self.lookback_days))
        sigma = sum(history, _ZERO) / len(history) if len(history) == self.lookback_days else None
        if self.day_open is not None:
            history.append(abs(close / self.day_open - 1))
        return sigma
