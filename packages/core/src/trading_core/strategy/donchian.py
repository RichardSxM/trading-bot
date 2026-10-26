"""``donchian_4h``'s pure rule (spec 0020.3, AC-7): a close beyond the previous bars' channel.

Each bar's channels are read before the bar itself joins the windows, so a close is always compared with
the highs and lows of earlier bars only. A close above the previous ``entry_bars`` bars' highest high is a
long, stopped ``stop_atr`` ATR below the close; a close below their lowest low is a short. In a position
the trade exits (``channel_exit``) on a close beyond the opposite channel of ``entry_bars // 2`` bars. The
signal is a level, not a stored setup. The time stop is the base's.
"""

from collections import deque
from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView

LONG_REASON: Final = "donchian_long"
SHORT_REASON: Final = "donchian_short"
CHANNEL_EXIT: Final = "channel_exit"


def _channel(highs: deque[Decimal], lows: deque[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if len(highs) < (highs.maxlen or 0):
        return None, None
    return max(highs), min(lows)


class DonchianRule:
    """The ``BarRule`` of ``donchian_4h``."""

    def __init__(self, *, entry_bars: int, stop_atr: Decimal) -> None:
        self.entry_bars = entry_bars
        self.exit_bars = entry_bars // 2
        self._stop_atr = stop_atr
        self._entry_highs: deque[Decimal] = deque(maxlen=entry_bars)
        self._entry_lows: deque[Decimal] = deque(maxlen=entry_bars)
        self._exit_highs: deque[Decimal] = deque(maxlen=self.exit_bars)
        self._exit_lows: deque[Decimal] = deque(maxlen=self.exit_bars)
        self._close: Decimal | None = None
        self.atr: Decimal | None = None
        self.entry_high: Decimal | None = None
        self.entry_low: Decimal | None = None
        self.exit_high: Decimal | None = None
        self.exit_low: Decimal | None = None

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Read both channels from the earlier bars, then add this bar's high and low to the windows."""
        self._close = bar.close
        if atr is not None:
            self.atr = atr
        self.entry_high, self.entry_low = _channel(self._entry_highs, self._entry_lows)
        self.exit_high, self.exit_low = _channel(self._exit_highs, self._exit_lows)
        self._entry_highs.append(bar.high)
        self._entry_lows.append(bar.low)
        self._exit_highs.append(bar.high)
        self._exit_lows.append(bar.low)

    def ready(self) -> bool:
        """True once the entry channel and an ATR exist, and never false again."""
        return self.entry_high is not None and self.atr is not None

    def entry(self) -> EnterSignal | None:
        """A long above the entry high or a short below the entry low, ``stop_atr`` ATR away, or ``None``."""
        close, atr = self._close, self.atr
        if close is None or atr is None or self.entry_high is None or self.entry_low is None:
            return None
        if close > self.entry_high:
            return EnterSignal(Side.LONG, close - self._stop_atr * atr, LONG_REASON)
        if close < self.entry_low:
            return EnterSignal(Side.SHORT, close + self._stop_atr * atr, SHORT_REASON)
        return None

    def consumed(self) -> None:
        """Nothing to clear: the signal is a level condition, not a stored setup."""

    def exit(self, trade: TradeView) -> str | None:
        """``channel_exit`` on a close beyond the opposite exit channel; none while it is not full."""
        close = self._close
        if close is None:
            return None
        if trade.side == Side.LONG and self.exit_low is not None and close < self.exit_low:
            return CHANNEL_EXIT
        if trade.side == Side.SHORT and self.exit_high is not None and close > self.exit_high:
            return CHANNEL_EXIT
        return None

    def on_flat(self) -> None:
        """Nothing to reset."""

    def values(self) -> dict[str, RuleValue]:
        """Both channels, each ``None`` until its window is full."""
        return {
            "entry_high": self.entry_high,
            "entry_low": self.entry_low,
            "exit_high": self.exit_high,
            "exit_low": self.exit_low,
        }
