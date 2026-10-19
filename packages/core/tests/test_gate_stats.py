"""covers: spec 0015 AC-11 (stationary bootstrap, Politis and White block length, CSCV PBO, deflated Sharpe)."""

import math
import random

import pytest

from trading_core.gate.stats import (
    deflated_sharpe,
    expected_max_sharpe,
    moments,
    pbo_cscv,
    politis_white_block,
    stationary_bootstrap_lower,
)


def _noise(n: int, seed: int, drift: float = 0.0, scale: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [drift + rng.gauss(0, scale) for _ in range(n)]


def test_expected_max_sharpe_matches_the_published_value_for_ten_trials():
    # Bailey and López de Prado: E[max] of 10 standard normal Sharpes ≈ 1.5747 (unit variance).
    assert expected_max_sharpe(10, 1.0) == pytest.approx(1.5747, abs=1e-3)
    assert expected_max_sharpe(1, 1.0) == 0.0
    assert expected_max_sharpe(10, 0.0) == 0.0


def test_one_trial_and_normal_returns_reduce_to_the_probabilistic_sharpe():
    # skew 0, kurtosis 3: Φ(0.1 * √100 / √(1 + 0.01 / 2)) = Φ(0.99751) ≈ 0.84073.
    assert deflated_sharpe(0.1, 101, 0.0, 3.0, 1, 0.0) == pytest.approx(0.84073, abs=1e-4)


def test_more_trials_deflate_the_same_sharpe():
    one = deflated_sharpe(0.1, 365, 0.0, 3.0, 1, 0.002)
    many = deflated_sharpe(0.1, 365, 0.0, 3.0, 50, 0.002)

    assert one is not None
    assert many is not None
    assert many < one


def test_deflated_sharpe_is_undefined_without_days_or_with_a_negative_variance_term():
    assert deflated_sharpe(0.1, 1, 0.0, 3.0, 1, 0.0) is None
    assert deflated_sharpe(2.0, 100, 5.0, 3.0, 1, 0.0) is None


def test_moments_of_a_symmetric_series():
    skew, kurt = moments([-2.0, -1.0, 0.0, 1.0, 2.0])

    assert skew == pytest.approx(0.0)
    assert kurt == pytest.approx(1.7)  # raw kurtosis of a discrete uniform on 5 points


def test_pure_noise_overfits_and_one_skilled_trial_does_not():
    noise = [_noise(200, seed) for seed in range(20)]
    matrix = [[col[i] for col in noise] for i in range(200)]
    skilled = [[*row[:-1], 0.01 + row[-1] * 0.1] for row in matrix]

    assert pbo_cscv(matrix, 10) > 0.30
    assert pbo_cscv(skilled, 10) == 0.0


def test_pbo_needs_an_even_block_count_and_enough_rows():
    with pytest.raises(ValueError, match="even block count"):
        pbo_cscv([[0.0] * 4] * 9, 10)
    with pytest.raises(ValueError, match="even block count"):
        pbo_cscv([[0.0] * 4] * 20, 9)


def test_the_bootstrap_is_seeded_and_bounds_a_clear_edge_above_zero():
    values = _noise(250, 3, drift=0.004)

    first = stationary_bootstrap_lower(values, 5, 2000, 0.95, seed=7)
    again = stationary_bootstrap_lower(values, 5, 2000, 0.95, seed=7)
    other = stationary_bootstrap_lower(values, 5, 2000, 0.95, seed=8)

    assert first == again
    assert first != other
    assert 0.0 < first.lower < sum(values) / len(values)


def test_a_constant_series_bounds_at_its_value():
    assert stationary_bootstrap_lower([0.002] * 30, 1, 500, 0.95, seed=1).lower == pytest.approx(0.002)


def test_politis_white_is_short_for_noise_and_longer_for_a_persistent_series():
    noise = _noise(500, 11)
    rng = random.Random(12)
    persistent = [0.0]
    for _ in range(499):
        persistent.append(0.95 * persistent[-1] + rng.gauss(0, 0.01))

    short = politis_white_block(noise, 1)
    long = politis_white_block(persistent, 1)

    assert short <= 5
    assert long > short
    assert long <= math.ceil(min(3 * math.sqrt(500), 500 / 3))
    assert politis_white_block(noise, 5) >= 5
