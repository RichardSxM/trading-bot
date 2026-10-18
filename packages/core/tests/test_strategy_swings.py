"""covers: spec 0014 AC-20 (swings are returned only on their confirming bar)."""

import random
from decimal import Decimal

import pytest

from trading_core.strategy.swings import Swing, SwingKind, SwingOrderError, SwingTracker

D = Decimal


def _feed(tracker, bars):
    out = []
    for i, (high, low) in enumerate(bars):
        out.extend(tracker.update(i, D(high), D(low)))
    return out


def test_a_swing_high_is_returned_on_bar_i_plus_right():
    tracker = SwingTracker(2, 2)
    bars = [(10, 5), (11, 6), (15, 7), (12, 6), (11, 5)]
    seen = [tracker.update(i, D(h), D(lo)) for i, (h, lo) in enumerate(bars)]
    assert seen[:4] == [[], [], [], []]
    assert seen[4] == [Swing(SwingKind.HIGH, 2, D(15), 4)]


def test_a_swing_low_mirrors_it():
    swings = _feed(SwingTracker(1, 1), [(10, 5), (9, 3), (10, 4)])
    assert swings == [Swing(SwingKind.LOW, 1, D(3), 2)]


def test_an_equal_high_is_not_a_swing():
    assert _feed(SwingTracker(1, 1), [(10, 5), (12, 6), (12, 6)]) == []


def test_no_swing_comes_before_its_confirming_bar_and_a_prefix_gives_the_confirmed_ones():
    rng = random.Random(14)
    bars = []
    price = 1000
    for _ in range(400):
        price += rng.randint(-5, 5)
        bars.append((price + rng.randint(0, 4), price - rng.randint(0, 4)))
    full = _feed(SwingTracker(3, 2), bars)
    assert full  # the series has swings
    assert all(s.confirmed_index == s.bar_index + 2 for s in full)
    for cut in (1, 7, 50, 199, 333):
        prefix = _feed(SwingTracker(3, 2), bars[:cut])
        assert prefix == [s for s in full if s.confirmed_index < cut]


def test_bars_must_be_fed_in_order():
    tracker = SwingTracker(1, 1)
    tracker.update(0, D(1), D(1))
    with pytest.raises(SwingOrderError):
        tracker.update(2, D(1), D(1))


def test_left_and_right_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        SwingTracker(0, 1)
