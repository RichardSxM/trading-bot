"""``bollinger_rsi``'s signal (spec 0017 child 0001): fade a 1 minute close back inside the band, in flat bands.

On each bar close ``t``: a long when the previous close was below the lower band with RSI at or under
``rsi_low`` and this close is back at or above the lower band; a short the mirror (above the upper
band, RSI at or over ``100 - rsi_low``). Both need flat bands: the middle moved at most
``flat_band_frac`` of the half band width over the last ``bb_n`` bars, and the bands have a width. The
stop sits past the excursion (the run of closes outside the band, at most ``bb_n`` bars, plus bar
``t``) by ``stop_atr_buffer`` ATRs; the target is the middle band at the signal.

Warm after ``2 * bb_n`` bars: the first signal can come on the next bar close.
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from trading_core.recording.enums import OrderSide
from trading_core.strategy.scalp import ScalpSignal
from trading_core.strategy.scalp_indicators import Atr, Bands, Bollinger, Rsi


@dataclass(frozen=True)
class _Bar:
    high: Decimal
    low: Decimal
    close: Decimal
    bands: Bands | None
    rsi: Decimal | None


class BollingerRsiState:
    """The indicators and the bar history the rule reads, fed one closed bar at a time."""

    def __init__(
        self,
        *,
        bb_n: int,
        bb_k: Decimal,
        rsi_n: int,
        rsi_low: int,
        atr_n: int,
        flat_band_frac: Decimal,
        stop_atr_buffer: Decimal,
    ) -> None:
        if not 0 < rsi_low < 50:
            raise ValueError(f"rsi_low must be inside (0, 50), got {rsi_low}")
        if flat_band_frac < 0 or stop_atr_buffer < 0:
            raise ValueError("flat_band_frac and stop_atr_buffer must not be negative")
        self.bb_n = bb_n
        self._bollinger = Bollinger(bb_n, bb_k)
        self._rsi = Rsi(rsi_n)
        self._atr = Atr(atr_n)
        self._rsi_low = Decimal(rsi_low)
        self._flat_band_frac = flat_band_frac
        self._stop_atr_buffer = stop_atr_buffer
        # Bar t and the bb_n bars before it.
        self._bars: deque[_Bar] = deque(maxlen=bb_n + 1)
        self.bars_seen = 0

    @property
    def warm(self) -> bool:
        """True once ``2 * bb_n`` bars came before the current one."""
        return self.bars_seen > 2 * self.bb_n

    def on_bar(self, high: Decimal, low: Decimal, close: Decimal) -> ScalpSignal | None:
        """Feed one closed bar; return the signal it gives, or None."""
        bands = self._bollinger.update(close)
        rsi = self._rsi.update(close)
        atr = self._atr.update(high, low, close)
        self._bars.append(_Bar(high, low, close, bands, rsi))
        self.bars_seen += 1
        if not self.warm or bands is None or atr is None:
            return None
        previous, first = self._bars[-2], self._bars[0]
        if previous.bands is None or previous.rsi is None or first.bands is None:
            return None
        drift = abs(bands.middle - first.bands.middle)
        half_width = bands.upper - bands.middle
        if half_width <= 0 or drift > self._flat_band_frac * half_width:
            return None
        inputs: dict[str, Decimal | int | str | None] = {
            "close": close,
            "lower": bands.lower,
            "middle": bands.middle,
            "upper": bands.upper,
            "rsi_prev": previous.rsi,
            "atr": atr,
            "band_drift": drift,
        }
        buffer = self._stop_atr_buffer * atr
        if previous.close < previous.bands.lower and previous.rsi <= self._rsi_low and close >= bands.lower:
            run = self._run(below=True)
            return ScalpSignal(OrderSide.BUY, min([low, *(b.low for b in run)]) - buffer, bands.middle, inputs)
        if previous.close > previous.bands.upper and previous.rsi >= 100 - self._rsi_low and close <= bands.upper:
            run = self._run(below=False)
            return ScalpSignal(OrderSide.SELL, max([high, *(b.high for b in run)]) + buffer, bands.middle, inputs)
        return None

    def _run(self, *, below: bool) -> list[_Bar]:
        """The consecutive bars before ``t`` (at most ``bb_n``) that closed outside the band on that side."""
        run: list[_Bar] = []
        for bar in list(self._bars)[-2::-1][: self.bb_n]:
            if bar.bands is None:
                break
            outside = bar.close < bar.bands.lower if below else bar.close > bar.bands.upper
            if not outside:
                break
            run.append(bar)
        return run
