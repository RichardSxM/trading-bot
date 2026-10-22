"""``connors_rsi``'s pure rule (spec 0018.1, AC-9): short period RSI extremes with the 200 bar trend.

Long when the close is above SMA(``trend_sma``) and RSI(``rsi_period``) is below ``rsi_lower``; short
when the close is below it and RSI is above ``100 - rsi_lower``. The structural stop is
``stop_atr_mult`` ATRs from the close. A long exits (``target``) at the first close above
SMA(``exit_sma``), a short at the first close below it. The signal is a condition on the current bar
only, so there is no setup state: ``consumed`` and ``on_flat`` do nothing.
"""

from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_indicators import Rsi, Sma
from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView

LONG_REASON: Final = "connors_long"
SHORT_REASON: Final = "connors_short"
TARGET: Final = "target"
_HUNDRED: Final = Decimal(100)


class ConnorsRsiRule:
    """The ``BarRule`` of ``connors_rsi``."""

    def __init__(
        self,
        bar_interval_ns: int,
        *,
        rsi_period: int,
        rsi_lower: int,
        trend_sma: int,
        exit_sma: int,
        stop_atr_mult: Decimal,
    ) -> None:
        self.bar_interval_ns = bar_interval_ns
        self._rsi = Rsi(rsi_period)
        self._trend = Sma(trend_sma)
        self._exit = Sma(exit_sma)
        self._lower = Decimal(rsi_lower)
        self._mult = stop_atr_mult
        self._close: Decimal | None = None
        self._atr: Decimal | None = None

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed one bar close and the base's ATR."""
        self._rsi.update(bar.close)
        self._trend.update(bar.close)
        self._exit.update(bar.close)
        self._close = bar.close
        self._atr = atr

    def ready(self) -> bool:
        """True once the RSI, both SMAs, and the ATR have values."""
        return None not in (self._rsi.value, self._trend.value, self._exit.value, self._atr)

    def entry(self) -> EnterSignal | None:
        """A long or short signal at this close, or ``None``."""
        rsi, trend, close, atr = self._rsi.value, self._trend.value, self._close, self._atr
        if rsi is None or trend is None or close is None or atr is None:
            return None
        if close > trend and rsi < self._lower:
            return EnterSignal(Side.LONG, close - self._mult * atr, LONG_REASON)
        if close < trend and rsi > _HUNDRED - self._lower:
            return EnterSignal(Side.SHORT, close + self._mult * atr, SHORT_REASON)
        return None

    def consumed(self) -> None:
        """Nothing to clear: the signal is a condition on the current bar."""

    def exit(self, trade: TradeView) -> str | None:
        """``target`` once the close is back beyond SMA(``exit_sma``) in the trade's favour."""
        sma, close = self._exit.value, self._close
        if sma is None or close is None:
            return None
        if trade.side == Side.LONG and close > sma:
            return TARGET
        if trade.side == Side.SHORT and close < sma:
            return TARGET
        return None

    def on_flat(self) -> None:
        """No trade state to reset."""

    def values(self) -> dict[str, RuleValue]:
        """The RSI and both SMAs."""
        return {"rsi": self._rsi.value, "sma_trend": self._trend.value, "sma_exit": self._exit.value}
