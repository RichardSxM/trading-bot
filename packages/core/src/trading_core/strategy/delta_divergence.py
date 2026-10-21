"""``delta_divergence``'s signal (spec 0017 child 0003): fade a strong few minute move
that aggressive volume does not back.

On each bar close ``t`` (minute ``m``): ``move = close[t] - close[t - lookback_bars]``. A short when the
move is at least ``move_atr`` ATRs up while the window's delta is at or below 0; a long the mirror. The
stop sits past the window's extreme (the last ``lookback_bars`` bars) by ``stop_atr_buffer`` ATRs; the
target is a half retrace, ``close[t] - move / 2``.
"""

from collections import deque
from decimal import Decimal

from trading_core.recording.enums import OrderSide
from trading_core.strategy.scalp import ScalpSignal
from trading_core.strategy.scalp_indicators import Atr, DeltaWindow


class DeltaDivergenceState:
    """The ATR, the delta window, and the bar history the rule reads."""

    def __init__(self, *, lookback_bars: int, move_atr: Decimal, atr_n: int, stop_atr_buffer: Decimal) -> None:
        if lookback_bars < 1 or move_atr <= 0 or stop_atr_buffer < 0:
            raise ValueError("lookback_bars must be at least 1, move_atr above 0, stop_atr_buffer not negative")
        self._lookback = lookback_bars
        self._move_atr = move_atr
        self._stop_atr_buffer = stop_atr_buffer
        self._atr = Atr(atr_n)
        self.window = DeltaWindow(lookback_bars)
        self._closes: deque[Decimal] = deque(maxlen=lookback_bars + 1)
        self._highs: deque[Decimal] = deque(maxlen=lookback_bars)
        self._lows: deque[Decimal] = deque(maxlen=lookback_bars)

    def on_trade(self, ts_event_ns: int, size: Decimal, aggressor: str) -> None:
        """One trade tick (``aggressor``: ``buy``, ``sell``, or ``none``)."""
        self.window.add(ts_event_ns, size, aggressor)

    def on_bar(self, minute: int, high: Decimal, low: Decimal, close: Decimal) -> ScalpSignal | None:
        """Feed the bar that closes ``minute``; return the signal it gives, or None."""
        delta = self.window.close(minute)
        atr = self._atr.update(high, low, close)
        self._closes.append(close)
        self._highs.append(high)
        self._lows.append(low)
        if delta is None or atr is None or len(self._closes) <= self._lookback:
            return None
        move = close - self._closes[0]
        inputs: dict[str, Decimal | int | str | None] = {
            "close": close,
            "move": move,
            "atr": atr,
            "delta": delta.delta,
            "buy_size": delta.buy_size,
            "sell_size": delta.sell_size,
        }
        threshold = self._move_atr * atr
        buffer = self._stop_atr_buffer * atr
        if move >= threshold and delta.delta <= 0:
            return ScalpSignal(OrderSide.SELL, max(self._highs) + buffer, close - move / 2, inputs)
        if move <= -threshold and delta.delta >= 0:
            return ScalpSignal(OrderSide.BUY, min(self._lows) - buffer, close - move / 2, inputs)
        return None
