"""covers: spec 0031 AC-17 (each check's boundary, days with no eligible time, best day removal)."""

from decimal import Decimal

from trading_core.lab.lts_verdict import APPROXIMATE_LABEL, DayMarkouts, evaluate_lts
from trading_core.marketdata.hours import DAY_NS


def day(n, mean, signals=25, eligible=DAY_NS):
    return DayMarkouts(f"d{n}", tuple(Decimal(mean) for _ in range(signals)), eligible)


def judge(days):
    return evaluate_lts(
        days,
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        hurdle_k=Decimal("1.5"),
        min_signals_per_day=25,
        min_positive_days=8,
    )


def checks(result):
    return {c.name: c for c in result.checks}


def test_cost_comes_from_the_fees():
    assert judge([day(0, 1)]).cost_bps == 7


def test_exactly_the_bar_passes_and_lists_every_check():
    result = judge([day(n, "10.5") for n in range(12)])

    assert result.verdict == "pass"
    assert result.label == APPROXIMATE_LABEL
    found = checks(result)
    assert found["pooled_mean_15s_bps"].value == Decimal("10.5")
    assert found["pooled_mean_15s_bps"].threshold == Decimal("10.5")
    assert found["signals_per_24_eligible_hours"].value == 25
    assert found["positive_days"].value == 12
    assert found["positive_days"].threshold == 8
    assert found["pooled_mean_without_best_day_bps"].threshold == 7


def test_just_below_the_bar_fails():
    result = judge([day(n, "10.4999") for n in range(12)])

    assert result.verdict == "fail"
    assert checks(result)["pooled_mean_15s_bps"].passed is False


def test_eight_positive_days_pass_and_seven_fail():
    eight = [day(n, 20) for n in range(8)] + [day(n, -1) for n in range(8, 12)]
    seven = [day(n, 20) for n in range(7)] + [day(n, -1) for n in range(7, 12)]

    assert checks(judge(eight))["positive_days"].passed is True
    assert checks(judge(seven))["positive_days"].passed is False


def test_twenty_five_signals_a_day_passes_and_twenty_four_fails():
    assert checks(judge([day(n, 11, signals=25) for n in range(12)]))["signals_per_24_eligible_hours"].passed is True
    assert checks(judge([day(n, 11, signals=24) for n in range(12)]))["signals_per_24_eligible_hours"].passed is False


def test_the_rate_uses_eligible_time_only():
    result = judge([day(0, 11, signals=25, eligible=DAY_NS // 2)])

    assert checks(result)["signals_per_24_eligible_hours"].value == 50


def test_a_day_with_no_eligible_hours_is_not_positive():
    days = [day(n, 20) for n in range(8)]
    days[7] = day(7, 20, eligible=0)

    assert checks(judge(days))["positive_days"].value == 7


def test_a_day_with_no_signals_is_not_positive():
    assert checks(judge([day(0, 20, signals=0)]))["positive_days"].value == 0


def test_removing_the_best_day_can_fail():
    days = [day(n, 1) for n in range(11)] + [day(11, 200)]

    found = checks(judge(days))

    assert found["pooled_mean_15s_bps"].passed is True
    assert found["pooled_mean_without_best_day_bps"].value == 1
    assert found["pooled_mean_without_best_day_bps"].passed is False


def test_the_best_day_is_the_largest_sum_and_ties_go_to_the_earliest():
    # Equal sums: the earliest day leaves, so the later day's mean of 2 remains.
    days = [day(0, 4, signals=25), day(1, 2, signals=50)]

    assert checks(judge(days))["pooled_mean_without_best_day_bps"].value == 2


def test_nothing_left_after_the_best_day_fails():
    assert checks(judge([day(0, 50)]))["pooled_mean_without_best_day_bps"].value is None
    assert checks(judge([day(0, 50)]))["pooled_mean_without_best_day_bps"].passed is False


def test_no_signals_at_all_fails_without_a_division_error():
    result = judge([day(n, 0, signals=0, eligible=0) for n in range(12)])

    assert result.verdict == "fail"
    assert all(c.passed is False for c in result.checks)
