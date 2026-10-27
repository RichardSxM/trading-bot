"""``noise_breakout``'s pure rule (spec 0020.1, AC-5): a close beyond the day's noise area and its VWAP.

Each bar is keyed by its open time (``ts_ns - bar_interval_ns``). The bands sit ``band_mult`` noise
``sigma`` around the UTC day's open (``NoiseBands``); ``long_line`` is the higher of the upper band and the
day's VWAP, ``short_line`` the lower of the lower band and the VWAP. A close above ``long_line`` is a long
with that line as its structural stop, a close below ``short_line`` a short. In a position the trade exits
(``noise_trail``) on the first close back inside its line: the current day's line on the entry day, the
entry day's last line, held fixed, after midnight. The signal is a level, not a stored setup, so it may
fire again on the next bar (unlimited re-entry). The time stop is the base's.
"""

from collections import deque
from datetime import date
from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView
from trading_core.strategy.noise_area import DayVwap, NoiseBands, utc_date

LONG_REASON: Final = "noise_long"
SHORT_REASON: Final = "noise_short"
NOISE_TRAIL: Final = "noise_trail"

type Lines = tuple[Decimal | None, Decimal | None]


class NoiseBreakoutRule:
    """The ``BarRule`` of ``noise_breakout``."""

    def __init__(self, bar_interval_ns: int, *, lookback_days: int, band_mult: Decimal, time_stop_bars: int) -> None:
        self.bar_interval_ns = bar_interval_ns
        self._band_mult = band_mult
        self._vwap = DayVwap()
        self._bands = NoiseBands(lookback_days, bar_interval_ns)
        # The open date of each recent bar, newest last, so ``bars_held`` finds the entry bar's date.
        self.days: deque[date] = deque(maxlen=time_stop_bars + 2)
        # Each recent day's last bar lines (``None`` included): the frozen lines after midnight.
        self.day_lines: dict[date, Lines] = {}
        self.warm = False
        self.bar_open_ns: int | None = None
        self._close: Decimal | None = None
        self.sigma: Decimal | None = None
        self.vwap: Decimal | None = None
        self.upper: Decimal | None = None
        self.lower: Decimal | None = None
        self.long_line: Decimal | None = None
        self.short_line: Decimal | None = None
        self.exit_line: Decimal | None = None

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed one bar close: the VWAP, the bands, both lines, and the day memory."""
        self.bar_open_ns = bar_open_ns = bar.ts_ns - self.bar_interval_ns
        self._close = bar.close
        self.exit_line = None  # set again only by this bar's exit check
        self.vwap = self._vwap.update(bar_open_ns, bar.high, bar.low, bar.close, bar.volume)
        self.sigma = self._bands.update(bar_open_ns, bar.open, bar.close)
        if self.sigma is not None:
            self.warm = True
        day_open = self._bands.day_open
        if day_open is not None and self.sigma is not None:
            self.upper = day_open * (1 + self._band_mult * self.sigma)
            self.lower = day_open * (1 - self._band_mult * self.sigma)
        else:
            self.upper = self.lower = None
        if self.upper is None or self.lower is None or self.vwap is None:
            self.long_line = self.short_line = None
        else:
            self.long_line, self.short_line = max(self.upper, self.vwap), min(self.lower, self.vwap)
        day = utc_date(bar_open_ns)
        self.days.append(day)
        self.day_lines[day] = (self.long_line, self.short_line)
        if len(self.day_lines) > len(set(self.days)):
            kept = set(self.days)
            self.day_lines = {d: lines for d, lines in self.day_lines.items() if d in kept}

    def ready(self) -> bool:
        """True from the first ready ``sigma`` on, and never false again."""
        return self.warm

    def entry(self) -> EnterSignal | None:
        """A long above ``long_line`` or a short below ``short_line``, stopped at that line, or ``None``."""
        close = self._close
        if close is None:
            return None
        if self.long_line is not None and close > self.long_line:
            return EnterSignal(Side.LONG, self.long_line, LONG_REASON)
        if self.short_line is not None and close < self.short_line:
            return EnterSignal(Side.SHORT, self.short_line, SHORT_REASON)
        return None

    def consumed(self) -> None:
        """Nothing to clear: the signal is a level condition, not a stored setup."""

    def _trade_line(self, trade: TradeView) -> Decimal | None:
        days = self.days
        entry_day = days[-1 - trade.bars_held] if trade.bars_held < len(days) else days[0]
        if entry_day == days[-1]:
            return self.long_line if trade.side == Side.LONG else self.short_line
        long_line, short_line = self.day_lines[entry_day]
        return long_line if trade.side == Side.LONG else short_line

    def exit(self, trade: TradeView) -> str | None:
        """``noise_trail`` on the first close back inside the trade's line; none while that line is ``None``."""
        close = self._close
        if close is None or not self.days:
            return None
        self.exit_line = line = self._trade_line(trade)
        if line is None:
            return None
        if trade.side == Side.LONG and close < line:
            return NOISE_TRAIL
        if trade.side == Side.SHORT and close > line:
            return NOISE_TRAIL
        return None

    def on_flat(self) -> None:
        """Nothing to reset."""

    def values(self) -> dict[str, RuleValue]:
        """The day, slot, day open, ``sigma``, bands, VWAP, both lines, and the last exit check's line."""
        bands = self._bands
        return {
            "day": None if bands.day is None else bands.day.isoformat(),
            "slot": bands.slot,
            "day_open": bands.day_open,
            "sigma": self.sigma,
            "upper": self.upper,
            "lower": self.lower,
            "vwap": self.vwap,
            "long_line": self.long_line,
            "short_line": self.short_line,
            "exit_line": self.exit_line,
        }
