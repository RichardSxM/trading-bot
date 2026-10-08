"""covers: spec 0007 AC-6 (oldest whole hours first, only before the backup cutoff, the state check)."""

import pytest

from trading_core.marketdata.errors import BackupStateInvalid
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.retention import HourFiles, parse_backup_state, plan_retention

H0 = 1_759_708_800 * SECOND_NS  # 2025-10-06 00:00 UTC
GB = 1_000_000_000


def _hours(n, data=10 * GB, quarantine=0):
    # Each hour's files were last written 10 minutes after the hour ended.
    return [
        HourFiles(
            hour_ns=H0 + i * HOUR_NS,
            data_bytes=data,
            total_bytes=data + quarantine,
            newest_mtime_ns=H0 + (i + 1) * HOUR_NS + 600 * SECOND_NS,
        )
        for i in range(n)
    ]


def _plan(hours, cutoff_ns, *, catalog, free, cap=30 * GB, floor=8 * GB):
    return plan_retention(hours, cutoff_ns, catalog_bytes=catalog, free_bytes=free, cap_bytes=cap, floor_bytes=floor)


def test_nothing_is_deleted_while_both_limits_hold():
    plan = _plan(_hours(3), H0 + 10 * HOUR_NS, catalog=30 * GB, free=8 * GB)
    assert plan.delete == ()
    assert not plan.blocked


def test_over_the_cap_with_the_cutoff_before_the_oldest_hour_is_blocked():
    plan = _plan(_hours(4), H0, catalog=40 * GB, free=50 * GB)
    assert plan.delete == ()
    assert plan.needed_bytes == 10 * GB
    assert plan.blocked


def test_the_oldest_hours_go_first_until_under_the_cap():
    plan = _plan(_hours(5), H0 + 10 * HOUR_NS, catalog=50 * GB, free=50 * GB)
    assert plan.delete == (H0, H0 + HOUR_NS)
    assert plan.needed_bytes == 0


def test_hours_are_taken_oldest_first_whatever_the_input_order():
    plan = _plan(list(reversed(_hours(5))), H0 + 10 * HOUR_NS, catalog=40 * GB, free=50 * GB)
    assert plan.delete == (H0,)


def test_it_stops_at_the_first_hour_not_yet_backed_up():
    # The cutoff covers only hour 0 (its files were last written at H0 + 1 h + 600 s).
    plan = _plan(_hours(5), H0 + HOUR_NS + 601 * SECOND_NS, catalog=50 * GB, free=50 * GB)
    assert plan.delete == (H0,)
    assert plan.needed_bytes == 10 * GB


def test_a_file_written_exactly_at_the_cutoff_is_not_backed_up():
    plan = _plan(_hours(2), H0 + HOUR_NS + 600 * SECOND_NS, catalog=40 * GB, free=50 * GB)
    assert plan.delete == ()


def test_the_free_floor_alone_triggers_deletion_and_quarantine_bytes_count():
    hours = _hours(4, data=1 * GB, quarantine=1 * GB)
    plan = _plan(hours, H0 + 10 * HOUR_NS, catalog=4 * GB, free=5 * GB)
    # Each hour frees 2 GB (its data and quarantine files): two hours bring 5 GB to 9 GB.
    assert plan.delete == (H0, H0 + HOUR_NS)
    assert plan.needed_bytes == 0


def test_no_cutoff_deletes_nothing():
    plan = _plan(_hours(3), None, catalog=40 * GB, free=2 * GB)
    assert plan.delete == ()
    assert plan.needed_bytes == 10 * GB  # the floor is 6 GB short, the cap 10 GB over


def test_running_out_of_hours_still_reports_what_is_needed():
    plan = _plan(_hours(1, data=1 * GB), H0 + 10 * HOUR_NS, catalog=1 * GB, free=2 * GB)
    assert plan.delete == (H0,)
    assert plan.needed_bytes == 5 * GB


NOW = H0 + 3 * HOUR_NS


def test_a_valid_state_parses_to_its_cutoff():
    state = parse_backup_state({"snapshot_id": "f0603109", "cutoff": "2025-10-06T02:30:00Z"}, NOW)
    assert (state.snapshot_id, state.cutoff_ns) == ("f0603109", H0 + 2 * HOUR_NS + 1_800 * SECOND_NS)


def test_a_cutoff_equal_to_now_is_valid():
    assert parse_backup_state({"snapshot_id": "a", "cutoff": "2025-10-06T03:00:00Z"}, NOW).cutoff_ns == NOW


@pytest.mark.parametrize(
    "raw",
    [
        [],
        "x",
        None,
        {"cutoff": "2025-10-06T02:30:00Z"},
        {"snapshot_id": "", "cutoff": "2025-10-06T02:30:00Z"},
        {"snapshot_id": 7, "cutoff": "2025-10-06T02:30:00Z"},
        {"snapshot_id": "a"},
        {"snapshot_id": "a", "cutoff": 1_759_717_800},
        {"snapshot_id": "a", "cutoff": "2025-10-06 02:30:00"},
        {"snapshot_id": "a", "cutoff": "2025-10-06T02:30:00+02:00"},
        {"snapshot_id": "a", "cutoff": "2025-10-06T03:00:01Z"},  # in the future
    ],
)
def test_an_invalid_state_raises(raw):
    with pytest.raises(BackupStateInvalid):
        parse_backup_state(raw, NOW)
