"""covers: spec 0015 AC-24 (replay study days: development, holdout, missing, dropped) and AC-2 (replay grid cap)."""

from datetime import date

import pytest

from trading_core.lab.errors import StudyDaysError
from trading_core.lab.segments import SkippedHour, day_start_ns
from trading_core.lab.study_days import (
    TARDIS_FIRST_DAY,
    development_days,
    dropped_days,
    firsts_of_month,
    holdout_days,
    import_command,
)
from trading_core.lab.variants import MAX_REPLAY_GRID_VARIANTS, VariantError, expand_grid
from trading_core.marketdata.hours import HOUR_NS

HOLDOUT_START = date(2025, 10, 19)


def test_firsts_of_month_cross_a_year_end():
    assert firsts_of_month(date(2025, 11, 15), date(2026, 2, 1)) == [
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]
    assert firsts_of_month(date(2025, 11, 2), date(2025, 11, 30)) == []


def test_development_from_the_first_tardis_day_gives_43_days_ending_before_the_holdout():
    days = development_days(TARDIS_FIRST_DAY, HOLDOUT_START)
    assert len(days) == 43
    assert days[0] == date(2022, 4, 1)
    assert days[-1] == date(2025, 10, 1)


def test_development_from_2024_gives_22_days():
    assert len(development_days(date(2024, 1, 1), HOLDOUT_START)) == 22
    assert len(development_days(date(2024, 2, 1), HOLDOUT_START)) == 21


@pytest.mark.parametrize("start", [date(2022, 4, 2), date(2022, 3, 1), date(2021, 12, 15)])
def test_a_from_that_is_not_a_whole_tardis_day_is_refused(start):
    with pytest.raises(StudyDaysError):
        development_days(start, HOLDOUT_START)


def test_holdout_is_every_first_up_to_the_latest_one_before_today():
    expected = firsts_of_month(date(2025, 11, 1), date(2026, 10, 1))
    assert len(expected) == 12
    assert holdout_days(HOLDOUT_START, date(2026, 10, 20)) == expected
    # On a 1st the day itself is not complete yet, so the holdout is still the same 12 days.
    assert holdout_days(HOLDOUT_START, date(2026, 11, 1)) == expected
    assert holdout_days(HOLDOUT_START, date(2026, 11, 2))[-1] == date(2026, 11, 1)


def test_holdout_before_its_first_day_is_empty():
    assert holdout_days(HOLDOUT_START, date(2025, 11, 1)) == []


def test_the_import_command_fetches_from_the_oldest_to_the_newest_missing_day():
    assert import_command([date(2022, 7, 1), date(2022, 5, 1)]) == "lab import-tardis 2022-05-01 2022-07-01"
    with pytest.raises(ValueError, match="no missing day"):
        import_command([])


def _hours(day, reason, count=24):
    return [SkippedHour(day_start_ns(day) + n * HOUR_NS, reason) for n in range(count)]


def test_a_day_gapped_in_every_hour_is_dropped():
    whole = date(2022, 5, 1)
    partly = date(2022, 6, 1)
    skipped = _hours(whole, "gap:ws_disconnect") + _hours(partly, "gap:ws_disconnect", count=23)
    assert dropped_days([date(2022, 4, 1), whole, partly], skipped) == [whole]


def test_a_day_without_segments_for_another_reason_is_not_dropped():
    day = date(2022, 5, 1)
    skipped = _hours(day, "gap:ws_disconnect", count=12) + [
        SkippedHour(day_start_ns(day) + n * HOUR_NS, "missing") for n in range(12, 24)
    ]
    assert dropped_days([day], skipped) == []


def test_replay_grid_is_capped_at_four_points_in_sorted_key_order():
    points = expand_grid({"slow": [2, 1], "fast": [4, 3]}, MAX_REPLAY_GRID_VARIANTS)
    assert points == [
        ("grid-1", {"fast": 4, "slow": 2}),
        ("grid-2", {"fast": 4, "slow": 1}),
        ("grid-3", {"fast": 3, "slow": 2}),
        ("grid-4", {"fast": 3, "slow": 1}),
    ]
    with pytest.raises(VariantError, match="at most 4"):
        expand_grid({"a": [1, 2, 3, 4, 5]}, MAX_REPLAY_GRID_VARIANTS)
