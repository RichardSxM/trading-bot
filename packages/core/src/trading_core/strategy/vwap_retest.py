"""``vwap_retest``'s signal (spec 0017 child 0002): in a 1 minute EMA trend, a pullback that
touches the day's VWAP and holds.

The VWAP anchors at 00:00 UTC of the first bar's day and again whenever a bar opens on a new UTC day.
On each bar close ``t`` with at least ``vwap_min_bars`` bars summed under the anchor: a long when the
9 EMA is above the 21 EMA, the previous close was above its VWAP, this bar's low reached the VWAP, and
it closed above it; a short the mirror. The stop is ``stop_atr`` ATRs past the VWAP; the target is
``target_r`` times the risk from the close, both fixed at the signal.
"""

from decimal import Decimal
from typing import Final

from trading_core.recording.enums import OrderSide
from trading_core.strategy.indicators import Ema
from trading_core.strategy.scalp import ScalpSignal
from trading_core.strategy.scalp_indicators import Atr, SessionVwap

DAY_NS: Final = 86_400_000_000_000


class VwapRetestState:
    """The EMAs, ATR, and session VWAP the rule reads, fed one closed bar at a time."""

    def __init__(self, *, atr_n: int, vwap_min_bars: int, stop_atr: Decimal, target_r: Decimal) -> None:
        if vwap_min_bars < 1 or stop_atr <= 0 or target_r <= 0:
            raise ValueError("vwap_min_bars must be at least 1, stop_atr and target_r above 0")
        self._fast = Ema(9)
        self._slow = Ema(21)
        self._atr = Atr(atr_n)
        self.vwap = SessionVwap()
        self._vwap_min_bars = vwap_min_bars
        self._stop_atr = stop_atr
        self._target_r = target_r
        self._previous: tuple[Decimal, Decimal | None] | None = None  # close and VWAP at t - 1

    def on_bar(self, open_ns: int, high: Decimal, low: Decimal, close: Decimal, volume: Decimal) -> ScalpSignal | None:
        """Feed one closed bar (``open_ns`` is its first nanosecond); return the signal it gives, or None."""
        day_start = open_ns - open_ns % DAY_NS
        if self.vwap.anchor_ns != day_start:
            self.vwap.anchor(day_start)
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        atr = self._atr.update(high, low, close)
        vwap = self.vwap.update(high, low, close, volume)
        previous, self._previous = self._previous, (close, vwap)
        if fast is None or slow is None or atr is None or vwap is None or previous is None:
            return None
        if self.vwap.bars < self._vwap_min_bars:
            return None
        previous_close, previous_vwap = previous
        if previous_vwap is None:
            return None
        inputs: dict[str, Decimal | int | str | None] = {
            "close": close,
            "vwap": vwap,
            "ema9": fast,
            "ema21": slow,
            "atr": atr,
            "bars_since_anchor": self.vwap.bars,
        }
        if fast > slow and previous_close > previous_vwap and low <= vwap < close:
            stop = vwap - self._stop_atr * atr
            return ScalpSignal(OrderSide.BUY, stop, close + self._target_r * (close - stop), {**inputs, "low": low})
        if fast < slow and previous_close < previous_vwap and close < vwap <= high:
            stop = vwap + self._stop_atr * atr
            return ScalpSignal(OrderSide.SELL, stop, close - self._target_r * (stop - close), {**inputs, "high": high})
        return None
