"""covers: spec 0015 AC-9 (anchored expanding folds per lab, the chosen grid point, the walk forward series)."""

from datetime import date, timedelta
from decimal import Decimal

from trading_core.gate.folds import add_months, candle_ranges, replay_ranges, walk_forward
from trading_core.gate.series import DailySeries

START = date(2024, 1, 1)


def _series(returns: list[float]) -> DailySeries:
    days = tuple(START + timedelta(days=i) for i in range(len(returns)))
    return DailySeries(days, tuple(returns), tuple(1 for _ in returns), tuple(Decimal(1) for _ in returns))


def test_months_clamp_to_the_month_end():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 11, 15), 3) == date(2025, 2, 15)


def test_candle_folds_expand_by_months_and_drop_a_short_tail():
    ranges = candle_ranges(date(2023, 1, 1), date(2025, 3, 31), 12, 6)

    assert [(r.is_start, r.is_end, r.oos_start, r.oos_end) for r in ranges] == [
        (date(2023, 1, 1), date(2023, 12, 31), date(2024, 1, 1), date(2024, 6, 30)),
        (date(2023, 1, 1), date(2024, 6, 30), date(2024, 7, 1), date(2024, 12, 31)),
    ]


def test_replay_folds_count_positions_in_the_shared_days():
    days = [START + timedelta(days=30 * i) for i in range(30)]
    ranges = replay_ranges(days, 15, 7)

    assert len(ranges) == 2  # 15 + 7 + 7 = 29 days used; the last day alone is a short step
    assert (ranges[0].is_end, ranges[0].oos_start) == (days[14], days[15])
    assert (ranges[1].oos_start, ranges[1].oos_end) == (days[22], days[28])


def test_the_in_sample_winner_is_scored_out_of_sample():
    # Point 0 wins in sample (the first 20 days) and loses after; point 1 the other way round.
    wins, loses = [0.01, 0.012] * 10, [-0.01, -0.008] * 10
    points = {0: _series(wins + loses), 1: _series(loses + wins)}
    ranges = replay_ranges([START + timedelta(days=i) for i in range(40)], 20, 10)

    wf = walk_forward(ranges, points)

    assert [f.chosen_grid_point for f in wf.folds] == [0, 0]
    assert wf.series.days == tuple(START + timedelta(days=i) for i in range(20, 40))
    assert all(r < 0 for r in wf.series.returns)
    assert wf.missing == ()


def test_a_fold_with_no_data_is_reported_missing():
    points = {0: _series([0.01] * 10)}
    ranges = replay_ranges([START + timedelta(days=i) for i in range(30)], 15, 7)

    assert walk_forward(ranges, points).missing == (1, 2)
