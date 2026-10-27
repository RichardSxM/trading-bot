"""covers: spec 0020 AC-7 (``donchian_4h``'s rules on hand built 4 hour bars), 0020.3 *Tests*.

The time stop is the base's (test_node_bar_rule), and the config's 60 bar time stop and its refusals are
in test_node_donchian_4h; here the rule's side: no ``channel_exit`` of its own while a quiet trade is held,
and the base never entering on its exit bar (``on_bar``'s ``exited`` check) leaves the next bar's channel
to decide a reversal.
"""

from datetime import UTC, datetime
from decimal import Decimal

from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side, TradeView
from trading_core.strategy.donchian import DonchianRule

D = Decimal
FOUR_HOURS_NS = 14_400_000_000_000
DAY0 = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp()) * 10**9
ATR = D(5)
TWO = D(2)


class _Bars:
    """Feeds 4 hour bars to a rule, one after the other; each bar's high and low sit 1 from its close."""

    def __init__(self, rule):
        self.rule = rule
        self.count = 0

    def feed(self, close, *, high=None, low=None, open_=None, volume=1, atr=ATR):
        c = D(close)
        h = c + 1 if high is None else D(high)
        lo = c - 1 if low is None else D(low)
        o = c if open_ is None else D(open_)
        self.count += 1
        self.rule.update(BarInput(DAY0 + self.count * FOUR_HOURS_NS, o, h, lo, c, D(volume)), atr)
        return self.rule.entry()

    def flat(self, bars, close=1000):
        for _ in range(bars):
            assert self.feed(close) is None


def _rule(entry_bars=20, stop_atr=TWO):
    return DonchianRule(entry_bars=entry_bars, stop_atr=stop_atr)


def _long(bars_held=1):
    return TradeView(Side.LONG, D(1002), D(992), bars_held)


def test_the_first_close_above_the_previous_20_bar_high_gives_a_long_stopped_2_atr_below_it():
    bars = _Bars(_rule())
    bars.flat(20)  # highs 1001, lows 999
    assert bars.feed(1002) == EnterSignal(Side.LONG, D(992), "donchian_long")
    assert bars.rule.values() == {
        "entry_high": D(1001),
        "entry_low": D(999),
        "exit_high": D(1001),
        "exit_low": D(999),
    }


def test_the_mirror_gives_a_short_stopped_2_atr_above_it():
    bars = _Bars(_rule())
    bars.flat(20)
    assert bars.feed(998) == EnterSignal(Side.SHORT, D(1008), "donchian_short")


def test_a_3_atr_grid_point_puts_the_stop_3_atr_away():
    bars = _Bars(_rule(stop_atr=D(3)))
    bars.flat(20)
    assert bars.feed(1002) == EnterSignal(Side.LONG, D(987), "donchian_long")


def test_a_close_equal_to_the_previous_high_or_low_gives_no_signal():
    bars = _Bars(_rule())
    bars.flat(20)
    assert bars.feed(1001, high=1001) is None
    assert bars.feed(999, low=999) is None


def test_the_current_bar_s_own_high_is_not_in_the_channel_it_is_compared_against():
    bars = _Bars(_rule())
    bars.flat(20)
    assert bars.feed(1050, high=1100) == EnterSignal(Side.LONG, D(1040), "donchian_long")
    assert bars.rule.values()["entry_high"] == D(1001)
    bars.feed(1000)
    assert bars.rule.values()["entry_high"] == D(1100)  # in the window from the next bar on


def test_a_long_exits_at_the_first_close_below_the_previous_10_bar_low():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)  # the entry bar, low 1001
    assert bars.feed(999, low=999) is None  # exit low 999 (9 flat bars and the entry bar): not below it
    assert bars.rule.exit(_long()) is None
    bars.feed("998.5", low="998.5")
    assert bars.rule.values()["exit_low"] == D(999)
    assert bars.rule.exit(_long(3)) == "channel_exit"


def test_a_short_exits_at_the_first_close_above_the_previous_10_bar_high():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(998)
    bars.feed("1001.5", high="1001.5")
    assert bars.rule.exit(TradeView(Side.SHORT, D(998), D(1008), 2)) == "channel_exit"


def test_the_exit_channel_is_half_the_entry_channel_rounded_down():
    bars = _Bars(_rule(entry_bars=55))
    assert bars.rule.exit_bars == 27
    bars.flat(27)
    bars.feed(1000)
    values = bars.rule.values()
    assert (values["exit_low"], values["entry_low"]) == (D(999), None)


def test_a_quiet_long_gets_no_rule_exit_so_only_the_base_s_time_stop_closes_it():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)
    for held in range(1, 61):
        bars.feed(1000)
        assert bars.rule.exit(_long(held)) is None


def test_no_signal_and_not_ready_before_20_earlier_bars():
    bars = _Bars(_rule())
    bars.flat(19)
    assert bars.feed(2000) is None  # only 19 earlier bars
    assert not bars.rule.ready()
    assert bars.rule.values()["entry_high"] is None
    assert bars.feed(2100) == EnterSignal(Side.LONG, D(2090), "donchian_long")  # 20 earlier bars now
    assert bars.rule.ready()


def test_ready_needs_an_atr_and_then_never_turns_false():
    bars = _Bars(_rule())
    for _ in range(21):
        bars.feed(1000, atr=None)
    assert not bars.rule.ready()
    assert bars.feed(1002, atr=None) is None
    bars.feed(1000)
    assert bars.rule.ready()
    bars.feed(1000, atr=None)
    assert bars.rule.ready()


def test_a_crash_bar_exits_the_long_and_the_next_bar_shorts_only_below_the_low_that_includes_it():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)
    # The crash bar: the rule's exit fires; its entry would read a short, but the base never asks for an
    # entry on the bar it exits on.
    bars.feed(900, low=899)
    assert bars.rule.exit(_long(1)) == "channel_exit"
    assert bars.feed(905) is None  # above the 20 bar low, which now holds the crash bar's 899
    assert bars.rule.values()["entry_low"] == D(899)
    assert bars.feed(898) == EnterSignal(Side.SHORT, D(908), "donchian_short")


def test_a_zero_volume_bar_joins_the_channels_like_any_other_bar():
    bars = _Bars(_rule())
    bars.flat(19)
    # What Nautilus builds for a 4 hour window with no trade (spec 0020 AC-2): volume 0, every price the last close.
    assert bars.feed(1000, high=1000, low=1000, volume=0) is None
    assert bars.feed(1002) == EnterSignal(Side.LONG, D(992), "donchian_long")  # the 20th earlier bar
    assert bars.rule.ready()
    assert bars.rule.values()["entry_low"] == D(999)


def test_consumed_and_on_flat_change_nothing():
    bars = _Bars(_rule())
    bars.flat(20)
    bars.feed(1002)
    signal = bars.rule.entry()
    bars.rule.consumed()
    bars.rule.on_flat()
    assert bars.rule.entry() == signal
