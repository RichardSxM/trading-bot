"""covers: spec 0008 AC-10 (fill rate and size weighted markouts per liquidity side, entry orders only)."""

from decimal import Decimal

import pytest

from trading_core.lab.fill_stats import EntryFill, EntryOrder, fill_stats
from trading_core.recording.enums import Liquidity

D = Decimal
MAKER, TAKER = Liquidity.MAKER, Liquidity.TAKER


def _fill(coid: str, qty: str, m1: str | None, liquidity: Liquidity = MAKER) -> EntryFill:
    marks = {1: None if m1 is None else D(m1), 5: D("1"), 30: D("1")}
    return EntryFill(coid, liquidity, D(qty), marks)


def test_an_unfilled_post_only_order_counts_as_placed_on_the_maker_side():
    orders = [EntryOrder("a", MAKER), EntryOrder("b", MAKER), EntryOrder("c", MAKER), EntryOrder("d", MAKER)]
    fills = [_fill("a", "1", "2"), _fill("a", "3", "-2"), _fill("b", "1", None)]

    (maker,) = fill_stats(orders, fills)

    assert (maker.liquidity, maker.orders_placed, maker.orders_filled, maker.fill_count) == (MAKER, 4, 2, 3)
    assert maker.fill_rate == D("0.5")
    # Weighted by size over the fills that have the horizon: (1 * 2 + 3 * -2) / 4 = -1.
    assert maker.markouts[1] == D("-1")
    assert maker.left_out == {1: 1, 5: 0, 30: 0}


def test_an_order_takes_the_side_of_its_fills_not_its_type():
    # A limit order that crossed and filled as taker belongs to the taker side.
    orders = [EntryOrder("a", MAKER), EntryOrder("m", TAKER)]
    fills = [_fill("a", "1", "1", TAKER), _fill("m", "1", "3", TAKER)]

    (taker,) = fill_stats(orders, fills)

    assert (taker.liquidity, taker.orders_placed, taker.fill_rate) == (TAKER, 2, D("1"))
    assert taker.markouts[1] == D("2")


def test_both_sides_appear_maker_first_and_a_horizon_with_no_fill_is_none():
    orders = [EntryOrder("t", TAKER), EntryOrder("k", MAKER)]
    fills = [_fill("t", "1", None, TAKER)]

    maker, taker = fill_stats(orders, fills)

    assert (maker.liquidity, maker.orders_filled, maker.markouts[1]) == (MAKER, 0, None)
    assert (taker.liquidity, taker.markouts[1], taker.markouts[5]) == (TAKER, None, D("1"))


def test_no_entry_order_gives_no_row():
    assert fill_stats([], []) == []


def test_a_fill_of_an_order_that_is_not_an_entry_is_refused():
    with pytest.raises(ValueError, match="not an entry order"):
        fill_stats([EntryOrder("a", MAKER)], [_fill("exit", "1", "1")])
