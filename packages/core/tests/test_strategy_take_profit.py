"""covers: the percent take profit of ``connors_rsi_tp``, judged on the side of the book a close would get."""

from decimal import Decimal as D

import pytest

from trading_core.strategy.bar_rule import Side
from trading_core.strategy.take_profit import take_profit_reached


@pytest.mark.parametrize(
    ("side", "bid", "ask", "reached"),
    [
        (Side.LONG, "100.50", "100.60", True),
        (Side.LONG, "100.49", "100.60", False),  # the mid is past the target, but a close gets the bid
        (Side.SHORT, "99.40", "99.50", True),
        (Side.SHORT, "99.40", "99.51", False),  # a short closes on the ask
        (Side.LONG, "99.00", "99.10", False),
        (Side.SHORT, "101.00", "101.10", False),
    ],
)
def test_the_target_is_read_from_the_price_a_close_would_get(side, bid, ask, reached):
    assert take_profit_reached(side, D(100), D("0.5"), D(bid), D(ask)) is reached
