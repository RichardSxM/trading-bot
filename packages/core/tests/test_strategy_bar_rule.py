"""covers: spec 0018 AC-2 (the stop floor on the mid, rounded away from it, and a bad signal), AC-4 (the R target)."""

from decimal import Decimal

import pytest

from trading_core.strategy.bar_rule import BadSignalError, Side, TradeView, floored_stop, r_target

D = Decimal


def test_a_structural_stop_inside_the_floor_is_moved_to_one_atr_and_rounded_down_for_a_long():
    # 0.2 ATR below the mid: the floor (mid - 1 x ATR) wins and is rounded down to the tick.
    assert floored_stop(Side.LONG, D("59990"), D("60000.5"), D(50), D(1), D(1)) == (D(59950), True)


def test_a_short_s_floor_is_rounded_up():
    assert floored_stop(Side.SHORT, D("60010"), D("60000.5"), D(50), D(1), D(1)) == (D(60051), True)


def test_a_structural_stop_beyond_the_floor_is_kept():
    assert floored_stop(Side.LONG, D("59800.4"), D(60000), D(50), D(1), D(1)) == (D(59800), False)
    assert floored_stop(Side.SHORT, D("60200.4"), D(60000), D(50), D(1), D(1)) == (D(60201), False)


def test_a_stop_exactly_at_the_floor_is_not_counted_as_floored():
    assert floored_stop(Side.LONG, D(59950), D(60000), D(50), D(1), D(1)) == (D(59950), False)


def test_the_floor_scales_with_its_multiple():
    assert floored_stop(Side.LONG, D(59990), D(60000), D(50), D("1.5"), D(1)) == (D(59925), True)


@pytest.mark.parametrize(("side", "stop"), [(Side.LONG, D(60000)), (Side.LONG, D(60100)), (Side.SHORT, D(60000))])
def test_a_stop_on_the_wrong_side_of_the_mid_is_a_bad_signal(side, stop):
    with pytest.raises(BadSignalError):
        floored_stop(side, stop, D(60000), D(50), D(1), D(1))


def test_a_floor_needs_positive_inputs():
    with pytest.raises(ValueError, match="bad stop floor"):
        floored_stop(Side.LONG, D(59000), D(60000), D(0), D(1), D(1))


def test_the_r_target_is_entry_plus_or_minus_r_times_the_risk():
    assert r_target(TradeView(Side.LONG, D(100), D(90), 3), D("1.5")) == D(115)
    assert r_target(TradeView(Side.SHORT, D(100), D(110), 3), D("2.5")) == D(75)
