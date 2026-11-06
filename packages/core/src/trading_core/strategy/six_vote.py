"""``SixVoteRule``: six votes on 1 hour bars, entering only when all six agree (spec 0029 AC-1).

Fed one bar close at a time with the base's Wilder ATR(14) (current bar included). Each vote is +1, -1,
or 0:

- V1 coin trend: sign(close - EMA200)
- V2 BTC trend: the sign last set by ``set_btc_trend``; 0 until set
- V3 9/21: sign(EMA9 - EMA21) when sign(close - EMA9) is the same, else 0
- V4 RSI: +1 above 55, -1 below 45, else 0 (RSI14, Wilder)
- V5 breakout: +1 when the close is above the highest high of the previous 20 bars, -1 below their lowest low
- V6 volume spike: volume above 3 x the mean of the previous 50 bars, range above 1.5 ATR, and the close in
  the top quarter (+1) or bottom quarter (-1) of the bar; a zero range bar votes 0

The definitions are pinned to the 2026-11-06 backtest: EMAs are ``ewm(span=n, adjust=False)`` (seeded with
the first close), RSI is ``ewm(alpha=1/14, adjust=False)`` of the up and down moves with a first move of 0,
``100 - 100 / (1 + up / down)``, and a down average of 0 gives 100. ``score`` is the sum. A long fires on
the bar where the score reaches 6 from below 6, a short where it reaches -6 from above -6; the stop is
3 ATR from the close. The rule never exits by itself. Pure: no I/O, no Nautilus.
"""

from collections import deque
from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import BarInput, EnterSignal, RuleValue, Side, TradeView

REASON: Final = "six_vote"
VOTES: Final = 6
EMA_FAST: Final = 9
EMA_SLOW: Final = 21
EMA_TREND: Final = 200
RSI_PERIOD: Final = 14
RSI_LONG: Final = Decimal(55)
RSI_SHORT: Final = Decimal(45)
BREAKOUT_BARS: Final = 20
VOLUME_BARS: Final = 50
VOLUME_MULT: Final = Decimal(3)
RANGE_ATR: Final = Decimal("1.5")
TOP_QUARTER: Final = Decimal("0.75")
BOTTOM_QUARTER: Final = Decimal("0.25")
STOP_ATR: Final = Decimal(3)
# Enough bars for every window: the EMA200, the 50 bar volume mean plus the current bar, the 20 bar window
# plus the current bar, and RSI14 plus its first move.
READY_BARS: Final = max(EMA_TREND, VOLUME_BARS + 1, BREAKOUT_BARS + 1, RSI_PERIOD + 1)
_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)


def sign(x: Decimal) -> int:
    """+1, -1, or 0 by the sign of ``x``."""
    return 1 if x > 0 else -1 if x < 0 else 0


def rsi_vote(rsi: Decimal) -> int:
    """V4: +1 when ``rsi`` is above 55, -1 below 45, else 0 (55 and 45 themselves vote 0)."""
    return 1 if rsi > RSI_LONG else -1 if rsi < RSI_SHORT else 0


class EwmEma:
    """``ewm(span=n, adjust=False)``: seeded with the first input, then ``v + 2 / (n + 1) * (x - v)``."""

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"an EMA needs n of at least 1, got {n}")
        self._alpha = Decimal(2) / Decimal(n + 1)
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal:
        """Feed one input; return the EMA after it."""
        self.value = x if self.value is None else self.value + self._alpha * (x - self.value)
        return self.value


class EwmRsi:
    """RSI as ``ewm(alpha=1/n, adjust=False)`` of the up and down moves, the first move being 0.

    A down average of 0 gives 100.
    """

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"an RSI needs n of at least 1, got {n}")
        self._alpha = Decimal(1) / Decimal(n)
        self._last: Decimal | None = None
        self._up = _ZERO
        self._down = _ZERO
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal:
        """Feed one close; return the RSI after it."""
        if self._last is None:
            self._up, self._down = _ZERO, _ZERO  # the first move is 0, which seeds both averages
        else:
            move = x - self._last
            self._up += self._alpha * (max(move, _ZERO) - self._up)
            self._down += self._alpha * (max(-move, _ZERO) - self._down)
        self._last = x
        self.value = _HUNDRED if self._down == 0 else _HUNDRED - _HUNDRED / (1 + self._up / self._down)
        return self.value


class SixVoteRule:
    """The six vote confluence rule (module docstring); a ``BarRule`` plus ``set_btc_trend``."""

    def __init__(self) -> None:
        self._ema9 = EwmEma(EMA_FAST)
        self._ema21 = EwmEma(EMA_SLOW)
        self._ema200 = EwmEma(EMA_TREND)
        self._rsi = EwmRsi(RSI_PERIOD)
        self._highs: deque[Decimal] = deque(maxlen=BREAKOUT_BARS)
        self._lows: deque[Decimal] = deque(maxlen=BREAKOUT_BARS)
        self._volumes: deque[Decimal] = deque(maxlen=VOLUME_BARS)
        self._bars = 0
        self._atr: Decimal | None = None
        self._btc = 0
        self._votes: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)
        self._score = 0
        self._pending: EnterSignal | None = None

    def set_btc_trend(self, trend: int) -> None:
        """Set V2 for the next ``update``: +1, -1, or 0 (BTC's sign(close - EMA200) at the same close time)."""
        if trend not in (-1, 0, 1):
            raise ValueError(f"a BTC trend is -1, 0, or 1, got {trend}")
        self._btc = trend

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed one bar close and the base's ATR; computes the votes, the score, and any signal."""
        # The windows hold the previous bars only, so read them before this bar joins them.
        breakout = self._breakout_vote(bar)
        volume = self._volume_vote(bar, atr)
        ema9 = self._ema9.update(bar.close)
        ema21 = self._ema21.update(bar.close)
        ema200 = self._ema200.update(bar.close)
        rsi = self._rsi.update(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._volumes.append(bar.volume)
        self._bars += 1
        self._atr = atr
        previous = self._score
        if not self.ready():
            self._votes, self._score, self._pending = (0, 0, 0, 0, 0, 0), 0, None
            return
        fast = sign(ema9 - ema21)
        self._votes = (
            sign(bar.close - ema200),
            self._btc,
            fast if fast == sign(bar.close - ema9) else 0,
            rsi_vote(rsi),
            breakout,
            volume,
        )
        self._score = sum(self._votes)
        assert atr is not None  # ready() needs it
        if self._score >= VOTES > previous:
            self._pending = EnterSignal(Side.LONG, bar.close - STOP_ATR * atr, REASON)
        elif self._score <= -VOTES < previous:
            self._pending = EnterSignal(Side.SHORT, bar.close + STOP_ATR * atr, REASON)
        else:
            self._pending = None

    def _breakout_vote(self, bar: BarInput) -> int:
        if len(self._highs) < BREAKOUT_BARS:
            return 0
        if bar.close > max(self._highs):
            return 1
        if bar.close < min(self._lows):
            return -1
        return 0

    def _volume_vote(self, bar: BarInput, atr: Decimal | None) -> int:
        if atr is None or len(self._volumes) < VOLUME_BARS:
            return 0
        span = bar.high - bar.low
        if span <= 0:
            return 0
        mean = sum(self._volumes, _ZERO) / Decimal(VOLUME_BARS)
        if not (bar.volume > VOLUME_MULT * mean and span > RANGE_ATR * atr):
            return 0
        where = (bar.close - bar.low) / span
        if where >= TOP_QUARTER:
            return 1
        if where <= BOTTOM_QUARTER:
            return -1
        return 0

    def ready(self) -> bool:
        """True once ``READY_BARS`` bars were fed and the ATR has a value."""
        return self._bars >= READY_BARS and self._atr is not None

    def entry(self) -> EnterSignal | None:
        """The signal at this bar (the score reached ±6 here), a pure read."""
        return self._pending

    def consumed(self) -> None:
        """Clear the signal that fired."""
        self._pending = None

    def exit(self, trade: TradeView) -> str | None:
        """Always ``None``: the time stop and the stop do the exiting."""
        return None

    def on_flat(self) -> None:
        """Nothing: the score crossing rule already needs a fresh cross."""

    @property
    def score(self) -> int:
        """The sum of the six votes at the last bar (0 before ready)."""
        return self._score

    def values(self) -> dict[str, RuleValue]:
        """The score, the six votes, and the EMAs and RSI behind them."""
        v = self._votes
        return {
            "score": self._score,
            "v_trend": v[0],
            "v_btc": v[1],
            "v_ema": v[2],
            "v_rsi": v[3],
            "v_breakout": v[4],
            "v_volume": v[5],
            "ema9": self._ema9.value,
            "ema21": self._ema21.value,
            "ema200": self._ema200.value,
            "rsi14": self._rsi.value,
        }
