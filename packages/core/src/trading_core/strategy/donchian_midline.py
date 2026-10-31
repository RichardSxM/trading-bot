"""``donchian_scalp_r1``'s pure rule (spec 0025 AC-1 to AC-3, AC-12): the Donchian breakout with the trend.

It wraps an unchanged ``DonchianRule`` for the channels and the breakout, and adds an EMA of every close.
A breakout counts only on the EMA's side: a long needs the close strictly above the EMA, a short strictly
below it. In a position the trade exits (``midline_exit``) on a close strictly beyond the middle of the
previous ``entry_bars`` bars' channel, read before the bar joins the window, so it trails bar by bar. The
inner rule's ``channel_exit`` is never asked. The time stop is the base's.
"""

from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView
from trading_core.strategy.donchian import DonchianRule
from trading_core.strategy.indicators import Ema

MIDLINE_EXIT: Final = "midline_exit"


class DonchianMidlineRule:
    """The ``BarRule`` of ``donchian_scalp_r1``."""

    def __init__(self, *, entry_bars: int, stop_atr: Decimal, ema_period: int) -> None:
        self.inner = DonchianRule(entry_bars=entry_bars, stop_atr=stop_atr)
        self.ema = Ema(ema_period)
        self._close: Decimal | None = None
        self.midline: Decimal | None = None

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed the inner rule (channels from earlier bars), then the EMA with the close, then the midline."""
        self._close = bar.close
        self.inner.update(bar, atr)
        self.ema.update(bar.close)
        high, low = self.inner.entry_high, self.inner.entry_low
        self.midline = None if high is None or low is None else (high + low) / 2

    def ready(self) -> bool:
        """True once the channel, the ATR, and the EMA all have a value, and never false again."""
        return self.inner.ready() and self.ema.value is not None

    def entry(self) -> EnterSignal | None:
        """The inner breakout, kept only when the close is strictly on the EMA's side of it, or ``None``."""
        signal, close, ema = self.inner.entry(), self._close, self.ema.value
        if signal is None or close is None or ema is None:
            return None
        if signal.side == Side.LONG and close > ema:
            return signal
        if signal.side == Side.SHORT and close < ema:
            return signal
        return None

    def consumed(self) -> None:
        """Nothing to clear: the signal is a level condition, not a stored setup."""

    def exit(self, trade: TradeView) -> str | None:
        """``midline_exit`` on a close strictly beyond the midline against the trade; none without a midline."""
        close, midline = self._close, self.midline
        if close is None or midline is None:
            return None
        if trade.side == Side.LONG and close < midline:
            return MIDLINE_EXIT
        if trade.side == Side.SHORT and close > midline:
            return MIDLINE_EXIT
        return None

    def on_flat(self) -> None:
        """Nothing to reset."""

    def values(self) -> dict[str, RuleValue]:
        """The entry channel, its midline, and the EMA, each ``None`` until it exists."""
        return {
            "entry_high": self.inner.entry_high,
            "entry_low": self.inner.entry_low,
            "midline": self.midline,
            "ema": self.ema.value,
        }
