"""covers: the dollar take profit and stop, judged on the side of the book a close would get."""

from decimal import Decimal as D

import pytest

from trading_core.strategy.bar_rule import Side
from trading_core.strategy.take_profit import STOP_LOSS, TAKE_PROFIT, dollar_exit


@pytest.mark.parametrize(
    ("side", "bid", "ask", "expected"),
    [
        (Side.LONG, "103.00", "103.10", TAKE_PROFIT),
        (Side.LONG, "102.99", "103.10", None),  # the ask is past the target, but a long closes on the bid
        (Side.LONG, "98.00", "98.10", STOP_LOSS),
        (Side.LONG, "98.01", "98.10", None),
        (Side.SHORT, "96.90", "97.00", TAKE_PROFIT),
        (Side.SHORT, "96.90", "97.01", None),  # a short closes on the ask
        (Side.SHORT, "101.90", "102.00", STOP_LOSS),
        (Side.SHORT, "101.90", "101.99", None),
    ],
)
def test_the_exit_is_read_from_the_price_a_close_would_get(side, bid, ask, expected):
    assert dollar_exit(side, D(100), D(1), D(bid), D(ask), D(3), D(2)) == expected


def test_a_bigger_position_reaches_the_same_dollars_on_a_smaller_move():
    assert dollar_exit(Side.LONG, D(100), D(10), D("100.30"), D("100.40"), D(3), D(2)) == TAKE_PROFIT
