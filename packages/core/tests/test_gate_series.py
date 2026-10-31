"""covers: spec 0015 AC-6 (daily returns per lab, the ruin rule, the daily Sharpe, and its rounding)."""

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_core.gate.series import DayEquity, annualized, daily_series, sharpe, to_decimal

START = date(2026, 1, 1)


def _rows(closes, trades=None):
    return [
        DayEquity(START + timedelta(days=i), Decimal(c), (trades or [0] * len(closes))[i], Decimal(0), Decimal(0))
        for i, c in enumerate(closes)
    ]


def test_a_candle_series_chains_each_close_and_starts_from_the_balance():
    series = daily_series(list(reversed(_rows(["1010", "1010", "999.9"]))), Decimal(1000), chained=True)

    assert series.days == (START, START + timedelta(days=1), START + timedelta(days=2))
    assert series.returns == pytest.approx((0.01, 0.0, (999.9 - 1010) / 1010))
    assert series.net_pnl == (Decimal(10), Decimal(0), Decimal("-10.1"))


def test_a_replay_series_measures_each_day_against_the_starting_balance():
    series = daily_series(_rows(["1010", "990"]), Decimal(1000), chained=False)

    assert series.returns == pytest.approx((0.01, -0.01))
    assert series.net_pnl == (Decimal(10), Decimal(-10))


def test_a_chained_series_scores_its_ruin_day_minus_one_and_zero_after():
    series = daily_series(_rows(["400", "-0.02", "-5", "10"]), Decimal(1000), chained=True)

    assert series.returns == pytest.approx((-0.6, -1.0, 0.0, 0.0))
    assert series.returns[1] == -1.0
    assert series.ruined_on == START + timedelta(days=1)
    assert series.compounded() == pytest.approx(-1.0)
    assert series.total_net() == Decimal(10) - Decimal(1000)
    assert series.between(START + timedelta(days=2), START + timedelta(days=3)).ruined_on is None
    assert series.between(START, START + timedelta(days=1)).ruined_on == START + timedelta(days=1)


def test_a_close_of_exactly_zero_is_ruin():
    series = daily_series(_rows(["0", "50"]), Decimal(1000), chained=True)

    assert series.returns == (-1.0, 0.0)
    assert series.ruined_on == START


def test_a_replay_day_loses_at_most_everything_and_the_next_day_starts_fresh():
    series = daily_series(_rows(["-200", "1010"]), Decimal(1000), chained=False)

    assert series.returns == pytest.approx((-1.0, 0.01))
    assert series.net_pnl == (Decimal(-1200), Decimal(10))
    assert series.ruined_on is None


def test_sharpe_is_mean_over_sample_std_and_annualizes_with_root_365():
    returns = [0.01, -0.005, 0.002, 0.004]
    mean = sum(returns) / 4
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / 3)

    assert sharpe(returns) == pytest.approx(mean / std)
    assert annualized(sharpe(returns)) == pytest.approx(mean / std * math.sqrt(365))


@pytest.mark.parametrize("returns", [[], [0.01], [0.01, 0.01, 0.01]])
def test_sharpe_is_undefined_under_two_days_or_with_zero_variance(returns):
    assert sharpe(returns) is None


def test_slices_and_day_limits():
    series = daily_series(_rows(["1001", "1002", "1003", "1004"], [1, 2, 3, 4]), Decimal(1000), chained=True)

    middle = series.between(START + timedelta(days=1), START + timedelta(days=2))
    assert middle.days == (START + timedelta(days=1), START + timedelta(days=2))
    assert middle.total_trades() == 5
    assert series.only([START, START + timedelta(days=3)]).total_net() == Decimal(2)


def test_statistics_round_half_even_to_six_places():
    assert to_decimal(0.12345650000000001) == Decimal("0.123457")
    assert to_decimal(None) is None
    with pytest.raises(ValueError, match="not finite"):
        to_decimal(math.inf)


def test_days_must_increase():
    rows = _rows(["1001", "1002"])
    with pytest.raises(ValueError, match="strictly increasing"):
        daily_series([rows[0], rows[0]], Decimal(1000), chained=True)
