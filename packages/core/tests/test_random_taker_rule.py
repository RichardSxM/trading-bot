"""covers: spec 0008 random_taker (UTC boundaries, a side draw from the seed and the boundary only)."""

from collections import Counter

import pytest

from trading_core.recording.enums import OrderSide
from trading_core.strategy.random_taker import NS_PER_S, draw_side, next_boundary_ns

MINUTE = 60 * NS_PER_S


def test_the_next_boundary_is_strictly_after_now():
    start = 1_791_482_100 * NS_PER_S  # 17:55:00 UTC, itself a boundary

    assert next_boundary_ns(start, 60) == start + MINUTE
    assert next_boundary_ns(start + 1, 60) == start + MINUTE
    assert next_boundary_ns(start - 1, 60) == start


def test_an_interval_of_zero_is_refused():
    with pytest.raises(ValueError, match="above zero"):
        next_boundary_ns(0, 0)


def test_a_draw_does_not_depend_on_earlier_draws():
    boundaries = [1_791_482_100 * NS_PER_S + n * MINUTE for n in range(50)]

    forward = [draw_side(7, b) for b in boundaries]
    backward = [draw_side(7, b) for b in reversed(boundaries)]

    assert forward == list(reversed(backward))
    assert draw_side(7, boundaries[3]) == forward[3]


def test_the_seed_changes_the_draws_and_both_sides_come_up():
    boundaries = [n * MINUTE for n in range(400)]

    sides = Counter(draw_side(7, b) for b in boundaries)

    assert set(sides) == {OrderSide.BUY, OrderSide.SELL}
    assert 150 < sides[OrderSide.BUY] < 250
    assert [draw_side(7, b) for b in boundaries] != [draw_side(8, b) for b in boundaries]
