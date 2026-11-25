"""covers: spec 0034 AC-1 (``RampParams``, ``ramp_target``) and AC-2 (``stop_hit``, ``stop_holds``)."""

from decimal import Decimal

import pytest

from trading_core.strategy.trend_vol_ramp import RampParams, ramp_target, stop_hit, stop_holds

D = Decimal


def test_params_refuse_bad_values():
    assert RampParams() == RampParams(3, D(8))
    with pytest.raises(ValueError, match="steps"):
        RampParams(steps=0)
    with pytest.raises(ValueError, match="stop_vols"):
        RampParams(stop_vols=D(0))


@pytest.mark.parametrize("may_grow", [True, False])
def test_a_zero_target_is_zero(may_grow):
    assert ramp_target(D(0), D(500), 3, may_grow) == 0


def test_from_flat_one_third_per_growing_round():
    assert ramp_target(D(900), D(0), 3, True) == D(300)
    assert ramp_target(D(900), D(300), 3, True) == D(600)
    assert ramp_target(D(900), D(600), 3, True) == D(900)
    assert ramp_target(D(-900), D(0), 3, True) == D(-300)
    assert ramp_target(D(-900), D(-300), 3, True) == D(-600)


def test_never_past_the_full_target():
    assert ramp_target(D(900), D(800), 3, True) == D(900)


def test_a_flat_coin_is_not_opened_when_it_may_not_grow():
    assert ramp_target(D(900), D(0), 3, False) == 0


def test_the_other_side_closes_and_opens_a_third_only_when_growing():
    assert ramp_target(D(900), D(-900), 3, True) == D(300)
    assert ramp_target(D(900), D(-900), 3, False) == 0
    assert ramp_target(D(-900), D(900), 3, True) == D(-300)


def test_a_cut_happens_at_once():
    assert ramp_target(D(600), D(900), 3, False) == D(600)
    assert ramp_target(D(-600), D(-900), 3, True) == D(-600)
    assert ramp_target(D(900), D(900), 3, False) == D(900)


def test_below_the_target_holds_when_it_may_not_grow():
    assert ramp_target(D(900), D(300), 3, False) == D(300)
    assert ramp_target(D(-900), D(-300), 3, False) == D(-300)


def test_one_step_is_the_full_target_when_growing():
    assert ramp_target(D(900), D(0), 1, True) == D(900)
    assert ramp_target(D(900), D(-200), 1, True) == D(900)
    assert ramp_target(D(900), D(200), 1, True) == D(900)


def test_stop_hit_long_and_short():
    # 8 vols of 2 % is 16 %: a long hits at -16 %, a short at +16 %.
    assert stop_hit(1, D(100), D(84), D("0.02"), D(8))
    assert not stop_hit(1, D(100), D("84.01"), D("0.02"), D(8))
    assert stop_hit(-1, D(100), D(116), D("0.02"), D(8))
    assert not stop_hit(-1, D(100), D("115.99"), D("0.02"), D(8))
    assert not stop_hit(1, D(100), D(200), D("0.02"), D(8))


def test_a_zero_entry_vol_never_hits():
    assert not stop_hit(1, D(100), D(1), D(0), D(8))


@pytest.mark.parametrize(
    ("side", "entry", "mark"), [(0, D(100), D(90)), (2, D(100), D(90)), (1, D(0), D(90)), (1, D(100), D(-1))]
)
def test_stop_hit_refuses_bad_input(side, entry, mark):
    with pytest.raises(ValueError, match="must be"):
        stop_hit(side, entry, mark, D("0.02"), D(8))


def test_stop_holds_only_while_every_signal_is_the_stopped_side():
    assert stop_holds([], 1)
    assert stop_holds([1, 1], 1)
    assert not stop_holds([1, 0], 1)
    assert not stop_holds([1, -1], 1)
    assert not stop_holds([None], 1)
    assert stop_holds([-1], -1)
