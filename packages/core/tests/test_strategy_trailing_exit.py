"""covers: the dollar trailing stop and hard stop, judged on the side of the book a close would get."""

from decimal import Decimal as D

import pytest

from trading_core.strategy.bar_rule import Side
from trading_core.strategy.trailing_exit import STOP_LOSS, TRAILING_STOP, open_pnl, trailing_exit

ARM, TRAIL, LOSS = D("1.5"), D("0.5"), D(3)


def _walk(pnls):
    peak, reasons = None, []
    for pnl in pnls:
        step = trailing_exit(D(pnl), peak, ARM, TRAIL, LOSS)
        peak = step.peak
        reasons.append(step.reason)
    return peak, reasons


@pytest.mark.parametrize(
    ("side", "bid", "ask", "expected"),
    [
        (Side.LONG, "101", "101.5", D(1)),
        (Side.SHORT, "98.5", "99", D(1)),
    ],
)
def test_open_profit_is_read_from_the_price_a_close_would_get(side, bid, ask, expected):
    assert open_pnl(side, D(100), D(1), D(bid), D(ask)) == expected


def test_a_gain_below_the_arm_level_never_trails_out():
    _, reasons = _walk(["1.4", "0.2", "-1", "1.0"])

    assert reasons == [None, None, None, None]


def test_once_armed_a_fall_of_the_trail_from_the_best_exits():
    peak, reasons = _walk(["1.0", "1.5", "2.2", "1.8", "1.7"])

    assert peak == D("2.2")
    assert reasons == [None, None, None, None, TRAILING_STOP]


def test_the_trail_follows_new_highs():
    _, reasons = _walk(["1.6", "1.2", "1.1"])

    assert reasons == [None, None, TRAILING_STOP]


def test_the_hard_stop_applies_before_the_trail_arms():
    _, reasons = _walk(["0.5", "-2.9", "-3"])

    assert reasons == [None, None, STOP_LOSS]


def test_a_quote_that_jumps_straight_past_the_trail_still_exits():
    _, reasons = _walk(["2.0", "-0.4"])

    assert reasons == [None, TRAILING_STOP]


def test_the_first_quote_sets_the_peak():
    step = trailing_exit(D("0.3"), None, ARM, TRAIL, LOSS)

    assert (step.pnl, step.peak, step.reason) == (D("0.3"), D("0.3"), None)
