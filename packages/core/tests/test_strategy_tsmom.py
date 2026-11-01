"""covers: spec 0027.2 AC-12 (``tsmom``'s rule on hand built daily closes).

The time stop, the stop floor, and sizing are the base's (test_node_bar_rule); here the rule's side: entry
only on a close strictly above the lagged close, ``momentum_exit`` at or below it, never a short, and a
re-entry on the very next qualifying close after any exit, since the signal is a level.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side, TradeView
from trading_core.strategy.tsmom import LONG_REASON, MOMENTUM_EXIT, TsmomRule, warmup_bars

D = Decimal
DAY_NS = 86_400 * 10**9
DAY0 = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp()) * 10**9
ATR = D(5)
LONG = TradeView(Side.LONG, D(1000), D(985), 1)


class _Days:
    """Feeds daily bars to a rule, one after the other; each bar's high and low sit 1 from its close."""

    def __init__(self, rule):
        self.rule = rule
        self.count = 0

    def feed(self, close, atr=ATR):
        c = D(close)
        self.count += 1
        self.rule.update(BarInput(DAY0 + self.count * DAY_NS, c, c + 1, c - 1, c, D(1)), atr)
        return self.rule.entry()


THREE = D(3)


def _rule(lookback_days=3, stop_atr=THREE, atr_period=20):
    return TsmomRule(lookback_days=lookback_days, stop_atr=stop_atr, atr_period=atr_period)


def test_it_is_ready_once_it_holds_lookback_plus_one_closes_and_an_atr():
    days = _Days(_rule())
    for close in (100, 101, 102):
        assert days.feed(close, atr=None) is None
        assert not days.rule.ready()
    days.feed(103, atr=None)
    assert not days.rule.ready()  # the window is full, but the base has no ATR yet
    days.feed(104)
    assert days.rule.ready()
    assert days.rule.values() == {
        "close": D(104),
        "close_lag": D(101),
        "momentum": D(104) / D(101) - 1,
        "lookback_days": 3,
    }


def test_it_enters_long_only_when_the_close_is_strictly_above_the_lagged_close():
    days = _Days(_rule())
    for close in (100, 90, 90):
        days.feed(close)
    assert days.feed(100) is None  # equal to the close 3 bars before: no entry
    signal = days.feed(91)  # above 90, three bars back
    assert signal == EnterSignal(Side.LONG, D(91) - 3 * ATR, LONG_REASON)


def test_the_stop_is_stop_atr_atrs_under_the_close():
    days = _Days(_rule(stop_atr=D("2.5")))
    for close in (100, 101, 102):
        days.feed(close)
    signal = days.feed(110, atr=D(4))
    assert signal is not None
    assert signal.structural_stop == D(110) - D("2.5") * D(4)


def test_a_falling_market_never_gives_a_short():
    days = _Days(_rule())
    for close in range(200, 100, -3):
        assert days.feed(close) is None


def test_it_exits_on_the_first_close_at_or_below_the_lagged_close():
    days = _Days(_rule())
    for close in (100, 101, 102, 103):
        days.feed(close)
    assert days.rule.exit(LONG) is None  # 103 above 100
    days.feed(101)  # lag 101: equal
    assert days.rule.exit(LONG) == MOMENTUM_EXIT
    days.feed(99)  # lag 102: below
    assert days.rule.exit(LONG) == MOMENTUM_EXIT


def test_it_enters_again_on_the_next_close_after_an_exit():
    """A stop (or any exit) never clears the level: the next close above the lag is an entry again."""
    days = _Days(_rule())
    for close in (100, 101, 102):
        days.feed(close)
    assert days.feed(110) is not None
    days.rule.consumed()
    days.rule.on_flat()  # the base's stop filled at this bar
    assert days.feed(111) is not None  # 111 above 101: the very next close re-enters


def test_no_exit_before_the_window_is_full():
    days = _Days(_rule())
    days.feed(100)
    assert days.rule.exit(LONG) is None


def test_warmup_bars_is_lookback_plus_atr_period_plus_one():
    assert warmup_bars(30, 20) == 51
    assert _rule(lookback_days=60, atr_period=20).warmup_bars() == 81


@pytest.mark.parametrize(
    ("lookback_days", "stop_atr", "problem"),
    [(0, D(3), "lookback_days"), (20, D(0), "stop_atr"), (20, D(-1), "stop_atr")],
)
def test_bad_settings_are_refused(lookback_days, stop_atr, problem):
    with pytest.raises(ValueError, match=problem):
        _rule(lookback_days=lookback_days, stop_atr=stop_atr)
