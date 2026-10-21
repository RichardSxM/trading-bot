"""covers: spec 0018 AC-9 (connors_rsi's rules on hand built bars).

The time stop is the base's, tested in test_node_bar_rule.
"""

from decimal import Decimal

from trading_core.strategy.bar_rule import BarInput, Side, TradeView
from trading_core.strategy.connors_rsi import ConnorsRsiRule

D = Decimal
ATR = D(10)
HOUR_NS = 3_600_000_000_000


def _rule():
    return ConnorsRsiRule(HOUR_NS, rsi_period=2, rsi_lower=5, trend_sma=200, exit_sma=5, stop_atr_mult=D(3))


def _feed(rule, closes, start=0):
    for i, c in enumerate(closes, start):
        rule.update(BarInput((i + 1) * HOUR_NS, D(c), D(c) + 5, D(c) - 5, D(c), D(1)), ATR)


# 205 closes rising 10 a bar, then three drops of 30: RSI(2) goes 25, 10, then 4.5 (below 5), while the
# close stays far above the 200 bar average (about 1000 below the last close).
RISE = [1000 + 10 * i for i in range(205)]
DIPS = [RISE[-1] - 30 * k for k in (1, 2, 3)]


def test_a_sharp_dip_above_the_200_bar_average_gives_one_long_with_a_3_atr_stop():
    rule = _rule()
    _feed(rule, RISE + DIPS[:2])
    assert rule.entry() is None  # RSI 10, not yet below 5
    _feed(rule, DIPS[2:], len(RISE) + 2)
    signal = rule.entry()
    assert signal is not None
    assert (signal.side, signal.reason) == (Side.LONG, "connors_long")
    assert signal.structural_stop == D(DIPS[2]) - 3 * ATR
    assert rule.values()["rsi"] < 5


def test_the_first_close_above_the_5_bar_average_exits_with_target():
    rule = _rule()
    closes = RISE + DIPS
    _feed(rule, closes)
    trade = TradeView(Side.LONG, D(DIPS[2]), D(DIPS[2]) - 30, 1)
    assert rule.exit(trade) is None  # still below SMA(5)
    _feed(rule, [DIPS[2] + 100], len(closes))
    assert rule.exit(trade) == "target"


def test_the_mirror_gives_a_short_below_the_average():
    rule = _rule()
    fall = [5000 - 10 * i for i in range(205)]
    pops = [fall[-1] + 30 * k for k in (1, 2, 3)]
    _feed(rule, fall + pops)
    signal = rule.entry()
    assert signal is not None
    assert (signal.side, signal.reason) == (Side.SHORT, "connors_short")
    assert signal.structural_stop == D(pops[2]) + 3 * ATR
    _feed(rule, [pops[2] - 100], len(fall) + 3)
    assert rule.exit(TradeView(Side.SHORT, D(pops[2]), D(pops[2]) + 30, 1)) == "target"


def test_the_same_dip_below_the_200_bar_average_gives_no_long():
    rule = _rule()
    fall = [5000 - 10 * i for i in range(205)]
    _feed(rule, fall + [fall[-1] - 30 * k for k in (1, 2, 3)])
    assert rule.entry() is None


def test_no_signal_before_200_bars():
    rule = _rule()
    _feed(rule, RISE[:197] + [RISE[196] - 30 * k for k in (1, 2, 3)])  # 200 bars: SMA(200) just ready
    assert rule.ready()
    rule = _rule()
    _feed(rule, RISE[:196] + [RISE[195] - 30 * k for k in (1, 2, 3)])  # 199 bars
    assert not rule.ready()
    assert rule.entry() is None


def test_consumed_and_on_flat_keep_a_condition_that_still_holds():
    rule = _rule()
    _feed(rule, RISE + DIPS)
    rule.consumed()
    rule.on_flat()
    assert rule.entry() is not None  # no setup state: the next close decides afresh
