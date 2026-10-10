"""covers: spec 0008 AC-3 (required days) and AC-4 (clean hours, skip reasons, segments cut at UTC midnight)."""

from datetime import date

import pytest

from trading_core.lab.segments import HourFiles, day_start_ns, hours_of, plan_segments, required_days
from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.hours import HOUR_NS

INSTRUMENT = "PF_XBTUSD.KRAKEN"
DAY = day_start_ns(date(2026, 10, 8))
CLEAN = HourFiles(has_deltas=True, has_trades=True, starts_with_clear=True)


def _hour(n: int) -> int:
    return DAY + n * HOUR_NS


def _all_clean(hours):
    return dict.fromkeys(hours, CLEAN)


def test_own_needs_every_day_and_tardis_only_first_of_month_days():
    assert required_days("own", date(2026, 9, 30), date(2026, 10, 2)) == [
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2026, 10, 2),
    ]
    assert required_days("tardis", date(2026, 9, 2), date(2026, 11, 1)) == [date(2026, 10, 1), date(2026, 11, 1)]
    assert required_days("tardis", date(2026, 9, 2), date(2026, 9, 30)) == []


def test_from_after_to_is_refused():
    with pytest.raises(ValueError, match="after"):
        required_days("own", date(2026, 10, 2), date(2026, 10, 1))


def test_clean_consecutive_hours_form_one_segment():
    hours = [_hour(17), _hour(18)]

    plan = plan_segments(hours, [], set(), _all_clean(hours))

    assert [s.hours for s in plan.segments] == [(_hour(17), _hour(18))]
    assert (plan.segments[0].index, plan.segments[0].start_ns, plan.segments[0].end_ns) == (
        1,
        _hour(17),
        _hour(19) - 1,
    )
    assert plan.skipped == ()


def test_each_unclean_hour_is_skipped_for_the_first_reason_and_splits_the_segments():
    hours = [_hour(n) for n in range(1, 8)]
    files = _all_clean(hours)
    files[_hour(4)] = HourFiles(has_deltas=True, has_trades=False, starts_with_clear=False)
    files[_hour(5)] = HourFiles(has_deltas=True, has_trades=True, starts_with_clear=False)
    # A gap in hour 2 that is also incomplete: the gap wins.
    gaps = [Gap(_hour(2) + 10, INSTRUMENT, "silence", _hour(2) + 20)]

    plan = plan_segments(hours, gaps, {_hour(2), _hour(3)}, files)

    assert [(h.hour_ns, h.reason) for h in plan.skipped] == [
        (_hour(2), "gap:silence"),
        (_hour(3), "incomplete"),
        (_hour(4), "missing"),
        (_hour(5), "no_snapshot"),
    ]
    assert [s.hours for s in plan.segments] == [(_hour(1),), (_hour(6), _hour(7))]
    assert [s.index for s in plan.segments] == [1, 2]
    assert plan.skipped_by_reason() == {"gap:silence": 1, "incomplete": 1, "missing": 1, "no_snapshot": 1}


def test_an_hour_without_a_files_entry_is_missing():
    plan = plan_segments([_hour(1)], [], set(), {})

    assert [(h.hour_ns, h.reason) for h in plan.skipped] == [(_hour(1), "missing")]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (_hour(3) - 5, _hour(3)),  # ends on the hour's first nanosecond
        (_hour(4) - 1, _hour(4) + 5),  # starts on the hour's last nanosecond
        (_hour(1), _hour(5)),  # spans several hours
    ],
)
def test_a_gap_touching_an_hour_at_its_edge_drops_it(start, end):
    hours = [_hour(3)]

    plan = plan_segments(hours, [Gap(start, INSTRUMENT, "reconnect", end)], set(), _all_clean(hours))

    assert plan.segments == ()
    assert plan.skipped[0].reason == "gap:reconnect"


def test_a_gap_from_the_day_before_reaching_into_the_range_drops_the_first_hour():
    hours = hours_of([date(2026, 10, 8)])
    gap = Gap(DAY - 600 * 10**9, INSTRUMENT, "recorder_down", DAY + 60 * 10**9)

    plan = plan_segments(hours, [gap], set(), _all_clean(hours))

    assert plan.skipped[0].hour_ns == DAY
    assert plan.segments[0].start_ns == _hour(1)


def test_clean_hours_from_22_to_02_split_at_midnight():
    hours = [_hour(22), _hour(23), _hour(24), _hour(25)]

    plan = plan_segments(hours, [], set(), _all_clean(hours))

    assert [s.hours for s in plan.segments] == [(_hour(22), _hour(23)), (_hour(24), _hour(25))]
    assert [s.index for s in plan.segments] == [1, 2]


def test_hours_must_be_whole_and_distinct():
    with pytest.raises(ValueError, match="whole hours"):
        plan_segments([_hour(1) + 1], [], set(), {})
    with pytest.raises(ValueError, match="whole hours"):
        plan_segments([_hour(1), _hour(1)], [], set(), {})
