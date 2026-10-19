"""covers: spec 0014 AC-13 (macd_baseline's entry and exit rules)."""

import random
from decimal import Decimal

import pytest

from trading_core.recording.enums import PositionSide
from trading_core.strategy.indicators import Ema, Macd
from trading_core.strategy.macd_baseline import MacdState, Signal

D = Decimal
FLAT, LONG, SHORT = PositionSide.FLAT, PositionSide.LONG, PositionSide.SHORT


def _series(seed=3, n=300):
    rng = random.Random(seed)
    price, out = 1000, []
    for _ in range(n):
        price += rng.randint(-12, 12)
        out.append(D(price))
    return out


@pytest.mark.parametrize("side", [FLAT, LONG, SHORT])
def test_signals_follow_the_histogram_and_the_trend_on_every_close(side):
    closes = _series()
    state, macd, trend = MacdState(3, 6, 4, 10), Macd(3, 6, 4), Ema(10)
    previous = None
    seen = set()
    for close in closes:
        signal = state.on_close(close, side)
        point, t = macd.update(close), trend.update(close)
        histogram = None if point is None else point.histogram
        expected = Signal.NONE
        if histogram is not None and previous is not None and t is not None:
            if side == FLAT and previous <= 0 < histogram and close > t:
                expected = Signal.ENTER_LONG
            elif side == FLAT and previous >= 0 > histogram and close < t:
                expected = Signal.ENTER_SHORT
            elif (side == LONG and histogram < previous) or (side == SHORT and histogram > previous):
                expected = Signal.EXIT
        assert signal == expected
        seen.add(signal)
        previous = histogram
    # The random walk exercises every signal this side can give.
    assert seen == (
        {Signal.NONE, Signal.ENTER_LONG, Signal.ENTER_SHORT} if side == FLAT else {Signal.NONE, Signal.EXIT}
    )


def test_no_signal_until_the_histogram_and_the_trend_ema_both_exist():
    state = MacdState(2, 3, 2, 50)
    # A dip and a sharp rise crosses the histogram up long before the trend EMA has 50 closes.
    closes = [D(100 - i) for i in range(10)] + [D(90 + 5 * i) for i in range(30)]
    assert {state.on_close(c, FLAT) for c in closes} == {Signal.NONE}
    assert not state.ready


def test_a_cross_up_below_the_trend_is_not_an_entry():
    state = MacdState(2, 3, 2, 5)
    # A long fall keeps the trend EMA high; one small bounce crosses the histogram up below it.
    closes = [D(200 - 10 * i) for i in range(12)] + [D(95), D(99)]
    signals = [state.on_close(c, FLAT) for c in closes]
    assert state.ready
    assert state.trend is not None
    assert closes[-1] < state.trend
    assert Signal.ENTER_LONG not in signals


def test_a_long_exits_when_the_histogram_weakens():
    state = MacdState(2, 3, 2, 3)
    rising = [D(100 + i * i) for i in range(10)]  # accelerating rise: the histogram keeps growing
    assert {state.on_close(c, LONG) for c in rising} == {Signal.NONE}
    assert state.on_close(D(150), LONG) == Signal.EXIT  # a sharp drop: the histogram falls
