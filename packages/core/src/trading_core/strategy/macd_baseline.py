"""``macd_baseline``'s pure rules (spec 0014 AC-13): when to enter and when to leave.

On each bar close it feeds the close to MACD (fast EMA - slow EMA, minus its signal EMA) and to a
trend EMA. While flat it enters long when the histogram crosses from ≤ 0 to > 0 and the close is above
the trend EMA, and short in the mirror case. While in a position it exits when the histogram weakens:
a long when the histogram is below its previous value, a short when it is above. No signal is given
until the histogram and the trend EMA both have a value. The indicators see every close, also while
a position is open, so the next entry reads the same history as it would without the trade.
"""

from decimal import Decimal
from enum import StrEnum

from trading_core.recording.enums import PositionSide
from trading_core.strategy.indicators import Ema, Macd


class Signal(StrEnum):
    """What the rule says at one bar close."""

    NONE = "none"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


class MacdState:
    """The indicators and the previous histogram, fed one bar close at a time."""

    def __init__(self, fast: int, slow: int, signal: int, trend_ema: int) -> None:
        self._macd = Macd(fast, slow, signal)
        self._trend = Ema(trend_ema)
        self.histogram: Decimal | None = None
        self.previous: Decimal | None = None
        self.trend: Decimal | None = None

    @property
    def ready(self) -> bool:
        """True once the histogram, its previous value, and the trend EMA all exist."""
        return self.histogram is not None and self.previous is not None and self.trend is not None

    def on_close(self, close: Decimal, side: PositionSide) -> Signal:
        """Feed one bar close; return the signal for a strategy whose position is ``side``."""
        point = self._macd.update(close)
        self.trend = self._trend.update(close)
        self.previous = self.histogram
        self.histogram = None if point is None else point.histogram
        if self.histogram is None or self.previous is None or self.trend is None:
            return Signal.NONE
        if side == PositionSide.FLAT:
            if self.previous <= 0 < self.histogram and close > self.trend:
                return Signal.ENTER_LONG
            if self.previous >= 0 > self.histogram and close < self.trend:
                return Signal.ENTER_SHORT
            return Signal.NONE
        if side == PositionSide.LONG and self.histogram < self.previous:
            return Signal.EXIT
        if side == PositionSide.SHORT and self.histogram > self.previous:
            return Signal.EXIT
        return Signal.NONE
