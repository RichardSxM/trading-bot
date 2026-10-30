"""covers: spec 0025 AC-1, AC-2, AC-3, AC-12 (``donchian_scalp_r1``'s pure rule on hand built bars).

Most tests use a short EMA so a few bars set it; the readiness test uses the real 200. The breakout itself is
``DonchianRule``'s (test_strategy_donchian); here the EMA side, the midline exit, readiness, and the values.
"""

from datetime import UTC, datetime
from decimal import Decimal

from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side, TradeView
from trading_core.strategy.donchian_midline import DonchianMidlineRule

D = Decimal
FIVE_MIN_NS = 300_000_000_000
DAY0 = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp()) * 10**9
ATR = D(5)
THREE = D(3)


class _Bars:
    """Feeds 5 minute bars to a rule; each bar's high and low sit 1 from its close."""

    def __init__(self, rule):
        self.rule = rule
        self.count = 0

    def feed(self, close, *, high=None, low=None, atr=ATR):
        c = D(close)
        h = c + 1 if high is None else D(high)
        lo = c - 1 if low is None else D(low)
        self.count += 1
        self.rule.update(BarInput(DAY0 + self.count * FIVE_MIN_NS, c, h, lo, c, D(1)), atr)
        return self.rule.entry()

    def flat(self, bars, close=1000):
        for _ in range(bars):
            assert self.feed(close) is None


def _rule(entry_bars=20, stop_atr=THREE, ema_period=3):
    return DonchianMidlineRule(entry_bars=entry_bars, stop_atr=stop_atr, ema_period=ema_period)


def _long(bars_held=1):
    return TradeView(Side.LONG, D(1002), D(987), bars_held)


def _short(bars_held=1):
    return TradeView(Side.SHORT, D(998), D(1013), bars_held)


def test_a_breakout_above_the_channel_with_the_close_above_the_ema_gives_a_long():
    bars = _Bars(_rule())
    bars.flat(20)  # highs 1001, EMA 1000
    assert bars.feed(1002) == EnterSignal(Side.LONG, D(987), "donchian_long")  # EMA 1001 now
    assert bars.rule.ema.value == D(1001)


def test_a_breakout_below_the_channel_with_the_close_below_the_ema_gives_a_short():
    bars = _Bars(_rule())
    bars.flat(20)
    assert bars.feed(998) == EnterSignal(Side.SHORT, D(1013), "donchian_short")


def test_a_breakout_against_the_ema_gives_nothing():
    bars = _Bars(_rule(ema_period=50))
    bars.flat(30, close=2000)
    bars.flat(20, close=1000)  # the EMA seeds at 1600; the channel holds only the 1000 bars
    assert bars.feed(1002) is None  # above the channel, below the EMA
    assert bars.rule.inner.entry() is not None  # the inner rule saw the breakout
    assert bars.rule.ema.value > D(1002)


def test_a_short_breakout_above_the_ema_gives_nothing():
    bars = _Bars(_rule(ema_period=50))
    bars.flat(30, close=500)
    bars.flat(20, close=1000)
    assert bars.feed(998) is None


def test_a_close_exactly_at_the_ema_gives_no_entry():
    # An EMA of 1 is the close itself, so every breakout closes exactly at it.
    bars = _Bars(_rule(ema_period=1))
    bars.flat(20)
    assert bars.feed(1002) is None
    assert bars.rule.ema.value == D(1002)
    assert bars.feed(990) is None


def test_a_long_exits_on_the_first_close_strictly_below_the_trailing_midline():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)  # the entry bar, high 1003
    assert bars.feed(1001) is None
    assert bars.rule.values()["midline"] == D(1001)  # (1003 + 999) / 2, the entry bar now in the window
    assert bars.rule.exit(_long()) is None  # equal to the midline: no exit
    bars.feed("1000.5")
    assert bars.rule.exit(_long(2)) == "midline_exit"


def test_a_short_exits_on_the_first_close_strictly_above_the_trailing_midline():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(998)  # low 997
    bars.feed(999)
    assert bars.rule.values()["midline"] == D(999)
    assert bars.rule.exit(_short()) is None
    bars.feed("999.5")
    assert bars.rule.exit(_short(2)) == "midline_exit"


def test_a_close_below_the_10_bar_exit_low_but_above_the_midline_does_not_exit():
    bars = _Bars(_rule())
    bars.flat(10, close=900)
    bars.flat(10, close=1000)
    assert bars.feed(1002) is not None
    bars.feed(990)  # below the inner 10 bar exit low (999), above the midline (951)
    assert bars.rule.inner.exit(_long()) == "channel_exit"
    assert bars.rule.values()["midline"] == D(951)
    assert bars.rule.exit(_long()) is None


def test_the_midline_never_reads_the_current_bar_s_own_high():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1050, high=1100)
    assert bars.rule.values()["midline"] == D(1000)
    bars.feed(1000)
    assert bars.rule.values()["midline"] == D("1049.5")


def test_not_ready_at_199_closes_and_ready_at_200_with_a_full_channel_and_an_atr():
    bars = _Bars(_rule(ema_period=200))
    bars.flat(199)
    assert not bars.rule.ready()
    assert bars.rule.inner.ready()
    assert bars.rule.values()["ema"] is None
    bars.feed(1000)
    assert bars.rule.ready()


def test_not_ready_without_an_atr_even_with_the_ema_and_channel():
    bars = _Bars(_rule())
    for _ in range(25):
        bars.feed(1000, atr=None)
    assert bars.rule.ema.value is not None
    assert not bars.rule.ready()


def test_values_are_exactly_the_channel_its_midline_and_the_ema():
    bars = _Bars(_rule())
    bars.feed(1000)
    assert bars.rule.values() == {"entry_high": None, "entry_low": None, "midline": None, "ema": None}
    bars.flat(20)
    assert bars.rule.values() == {
        "entry_high": D(1001),
        "entry_low": D(999),
        "midline": D(1000),
        "ema": D(1000),
    }


def test_consumed_and_on_flat_change_nothing():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)
    signal = bars.rule.entry()
    bars.rule.consumed()
    bars.rule.on_flat()
    assert bars.rule.entry() == signal
