"""``tsmom``'s pure rule (spec 0027.2 AC-12): daily time series momentum, long only.

Long while the daily close is strictly above the close ``lookback_days`` bars before it, stopped
``stop_atr`` ATR below the close; flat otherwise. In a position the trade exits (``momentum_exit``) on
the first close at or below that lagged close. The signal is a level, not a stored setup, so after any
exit the next close that meets the condition enters again. The rule never gives a short, and it holds
no position state: the base asks ``entry()`` only when flat and ``exit()`` only in a position. The
time stop is the base's. Frozen after the first study (spec 0018 AC-7): a change goes into a new module.
"""

from collections import deque
from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView

LONG_REASON: Final = "tsmom_long"
MOMENTUM_EXIT: Final = "momentum_exit"


def warmup_bars(lookback_days: int, atr_period: int) -> int:
    """Bars a cold start needs before the first decision (spec 0027.1 AC-10a): ``lookback_days + atr_period + 1``.

    Enough for the lagged close and for the base's ATR, with one bar to spare.
    """
    return lookback_days + atr_period + 1


class TsmomRule:
    """The ``BarRule`` of ``tsmom``."""

    def __init__(self, *, lookback_days: int, stop_atr: Decimal, atr_period: int) -> None:
        if lookback_days < 1:
            raise ValueError(f"lookback_days must be at least 1, got {lookback_days}")
        if stop_atr <= 0:
            raise ValueError(f"stop_atr must be above 0, got {stop_atr}")
        self.lookback_days = lookback_days
        self._stop_atr = stop_atr
        self._atr_period = atr_period
        self._closes: deque[Decimal] = deque(maxlen=lookback_days + 1)
        self.atr: Decimal | None = None

    def warmup_bars(self) -> int:
        """Bars before the rule can decide from a cold start (the lab's trend pre roll reads it)."""
        return warmup_bars(self.lookback_days, self._atr_period)

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Add this bar's close to the window and keep the latest ATR."""
        self._closes.append(bar.close)
        if atr is not None:
            self.atr = atr

    @property
    def close(self) -> Decimal | None:
        """The latest close, or ``None`` before the first bar."""
        return self._closes[-1] if self._closes else None

    @property
    def close_lag(self) -> Decimal | None:
        """The close ``lookback_days`` bars before the latest, or ``None`` until the window is full."""
        return self._closes[0] if len(self._closes) == self._closes.maxlen else None

    def ready(self) -> bool:
        """True once the window holds ``lookback_days + 1`` closes and an ATR exists, and never false again."""
        return self.close_lag is not None and self.atr is not None

    def entry(self) -> EnterSignal | None:
        """A long when the close is strictly above the lagged close, ``stop_atr`` ATR below the close, else ``None``."""
        close, lag, atr = self.close, self.close_lag, self.atr
        if close is None or lag is None or atr is None:
            return None
        if close > lag:
            return EnterSignal(Side.LONG, close - self._stop_atr * atr, LONG_REASON)
        return None

    def consumed(self) -> None:
        """Nothing to clear: the signal is a level condition, not a stored setup."""

    def exit(self, trade: TradeView) -> str | None:
        """``momentum_exit`` on a close at or below the lagged close; none while the window is not full."""
        close, lag = self.close, self.close_lag
        if close is None or lag is None:
            return None
        return MOMENTUM_EXIT if close <= lag else None

    def on_flat(self) -> None:
        """Nothing to reset."""

    def values(self) -> dict[str, RuleValue]:
        """The close, the lagged close, the momentum (``close / close_lag - 1``), and the lookback."""
        close, lag = self.close, self.close_lag
        momentum = None if close is None or lag is None or lag == 0 else close / lag - 1
        return {"close": close, "close_lag": lag, "momentum": momentum, "lookback_days": self.lookback_days}
