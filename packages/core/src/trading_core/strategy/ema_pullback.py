"""``ema_pullback``'s pure rule (spec 0019.2, AC-6): a pullback to the fast EMA inside a short term trend.

The trend is up while EMA(``fast``) is above EMA(``slow``), down while below. In an up trend a bar whose
low reaches EMA(``fast``) with RSI(``rsi_period``) from ``rsi_low`` to ``rsi_high`` arms a long; the
first close back above EMA(``fast``) within ``arm_bars`` bars (the arming bar counts as 1, and may
trigger itself) is the signal. The arm is dropped when the trend stops being up or a close falls below
EMA(``slow``). The structural stop is the lower of the pullback's lowest low and EMA(``slow``); the exit
(``target``) is the first close at or beyond ``target_r`` times the trade's risk. Shorts are the mirror.
Steps 1 to 4 run in ``update`` on every bar, also in a position; ``entry`` only reads.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Final

from trading_core.strategy.bar_indicators import Rsi
from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView, r_target
from trading_core.strategy.indicators import Ema

LONG_REASON: Final = "ema_pullback_long"
SHORT_REASON: Final = "ema_pullback_short"
TARGET: Final = "target"
_HUNDRED: Final = Decimal(100)


class Trend(StrEnum):
    """Which way EMA(fast) sits from EMA(slow)."""

    UP = "up"
    DOWN = "down"
    NONE = "none"


class EmaPullbackRule:
    """The ``BarRule`` of ``ema_pullback``."""

    def __init__(
        self,
        bar_interval_ns: int,
        *,
        fast: int,
        slow: int,
        rsi_period: int,
        rsi_low: int,
        rsi_high: int,
        arm_bars: int,
        target_r: Decimal,
    ) -> None:
        self.bar_interval_ns = bar_interval_ns
        self._fast = Ema(fast)
        self._slow = Ema(slow)
        self._rsi = Rsi(rsi_period)
        self._rsi_low = Decimal(rsi_low)
        self._rsi_high = Decimal(rsi_high)
        self._arm_bars = arm_bars
        self._target_r = target_r
        self._close: Decimal | None = None
        self._atr: Decimal | None = None
        self.trend = Trend.NONE
        self.armed: Side | None = None
        self.pullback_extreme: Decimal | None = None
        self.armed_bars = 0

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed one bar close and the base's ATR, then run the trend, disarm, arm, and track steps."""
        fast = self._fast.update(bar.close)
        slow = self._slow.update(bar.close)
        rsi = self._rsi.update(bar.close)
        self._close = bar.close
        self._atr = atr
        if fast is None or slow is None:
            self.trend = Trend.NONE
        else:
            self.trend = Trend.UP if fast > slow else Trend.DOWN if fast < slow else Trend.NONE
        if self.armed is not None:
            self.armed_bars += 1
            if self._disarms(bar.close, slow):
                self.consumed()
            elif self.pullback_extreme is not None:
                if self.armed == Side.LONG:
                    self.pullback_extreme = min(self.pullback_extreme, bar.low)
                else:
                    self.pullback_extreme = max(self.pullback_extreme, bar.high)
        if self.armed is None and fast is not None and rsi is not None:
            self._arm(bar, fast, rsi)

    def _disarms(self, close: Decimal, slow: Decimal | None) -> bool:
        if self.armed_bars > self._arm_bars or slow is None:
            return True
        if self.armed == Side.LONG:
            return self.trend != Trend.UP or close < slow
        return self.trend != Trend.DOWN or close > slow

    def _arm(self, bar: BarInput, fast: Decimal, rsi: Decimal) -> None:
        if self.trend == Trend.UP and bar.low <= fast and self._rsi_low <= rsi <= self._rsi_high:
            self.armed, self.pullback_extreme, self.armed_bars = Side.LONG, bar.low, 1
        elif (
            self.trend == Trend.DOWN
            and bar.high >= fast
            and _HUNDRED - self._rsi_high <= rsi <= _HUNDRED - self._rsi_low
        ):
            self.armed, self.pullback_extreme, self.armed_bars = Side.SHORT, bar.high, 1

    def ready(self) -> bool:
        """True once both EMAs, the RSI, and the ATR have values."""
        return None not in (self._fast.value, self._slow.value, self._rsi.value, self._atr)

    def entry(self) -> EnterSignal | None:
        """The armed side's signal once the close is back beyond EMA(fast), or ``None``."""
        fast, slow, close, extreme = self._fast.value, self._slow.value, self._close, self.pullback_extreme
        if fast is None or slow is None or close is None or extreme is None:
            return None
        if self.armed == Side.LONG and close > fast:
            return EnterSignal(Side.LONG, min(extreme, slow), LONG_REASON)
        if self.armed == Side.SHORT and close < fast:
            return EnterSignal(Side.SHORT, max(extreme, slow), SHORT_REASON)
        return None

    def consumed(self) -> None:
        """Disarm: clear the side, the pullback extreme, and the bar count."""
        self.armed, self.pullback_extreme, self.armed_bars = None, None, 0

    def exit(self, trade: TradeView) -> str | None:
        """``target`` once the close reaches ``target_r`` times the trade's risk in its favour."""
        close = self._close
        if close is None:
            return None
        target = r_target(trade, self._target_r)
        if trade.side == Side.LONG and close >= target:
            return TARGET
        if trade.side == Side.SHORT and close <= target:
            return TARGET
        return None

    def on_flat(self) -> None:
        """Nothing to reset: an arm that has not triggered can still fire after the trade."""

    def values(self) -> dict[str, RuleValue]:
        """Both EMAs, the RSI, the trend, and the arm."""
        return {
            "ema_fast": self._fast.value,
            "ema_slow": self._slow.value,
            "rsi": self._rsi.value,
            "trend": self.trend.value,
            "armed": "none" if self.armed is None else self.armed.value,
            "pullback_extreme": self.pullback_extreme,
            "armed_bars": self.armed_bars,
        }
