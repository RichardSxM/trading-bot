"""covers: the daily book take profit and stop, each a share of the book's planned size."""

from decimal import Decimal as D

import pytest

from trading_core.strategy.book_exit import BOOK_STOP_LOSS, BOOK_TAKE_PROFIT, book_exit, book_notional


@pytest.mark.parametrize(
    ("day_pnl", "expected"),
    [("300", BOOK_TAKE_PROFIT), ("299.99", None), ("0", None), ("-199.99", None), ("-200", BOOK_STOP_LOSS)],
)
def test_the_day_closes_at_either_limit_and_not_before(day_pnl, expected):
    assert book_exit(D(day_pnl), D(10000), D("0.03"), D("0.02")) == expected


def test_a_limit_that_is_not_set_is_off():
    assert book_exit(D(900), D(10000), None, D("0.02")) is None
    assert book_exit(D(-900), D(10000), D("0.03"), None) is None
    assert book_exit(D(-900), D(10000), None, D("0.02")) == BOOK_STOP_LOSS


def test_the_limits_follow_the_book_size_not_the_equity():
    assert book_exit(D(300), D(50000), D("0.03"), D("0.03")) is None
    assert book_exit(D(1500), D(50000), D("0.03"), D("0.03")) == BOOK_TAKE_PROFIT


def test_an_empty_book_never_exits():
    assert book_exit(D(-5000), D(0), D("0.03"), D("0.03")) is None


@pytest.mark.parametrize(
    ("notional", "take", "stop"), [("-1", "0.03", "0.03"), ("100", "0", "0.03"), ("100", "0.03", "-1")]
)
def test_bad_inputs_are_refused(notional, take, stop):
    with pytest.raises(ValueError, match="must be"):
        book_exit(D(1), D(notional), D(take), D(stop))


def test_the_book_size_ignores_the_side_of_each_target():
    assert book_notional([D(8000), D(-3000), D(0)]) == D(11000)
