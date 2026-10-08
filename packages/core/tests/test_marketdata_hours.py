"""covers: spec 0007 AC-2 (hour bounds), the ready rule of *Lifecycle of one hour*, AC-9 (backlog space),
AC-5 (day close)."""

from decimal import Decimal

from trading_core.marketdata.hours import (
    DAY_NS,
    GB,
    HOUR_NS,
    SECOND_NS,
    backlog_bytes_needed,
    day_closed,
    day_name,
    day_of,
    hour_last_ns,
    hour_of,
    hour_ready,
    hours_inside,
)

# 2026-10-08 06:00:00 UTC
H = 1_791_439_200 * SECOND_NS
GRACE = 300 * SECOND_NS


def test_hour_of_floors_to_the_hour_start():
    assert hour_of(H) == H
    assert hour_of(H + 1) == H
    assert hour_of(H + HOUR_NS - 1) == H
    assert hour_of(H + HOUR_NS) == H + HOUR_NS


def test_hour_last_ns_is_one_before_the_next_hour():
    assert hour_last_ns(H) == H + HOUR_NS - 1
    assert hour_of(hour_last_ns(H)) == H


def test_not_ready_before_the_grace_has_passed():
    positions = {"deltas": H + HOUR_NS}
    assert not hour_ready(H, now_ns=H + HOUR_NS + GRACE - 1, grace_ns=GRACE, positions=positions, run_ended=False)
    assert hour_ready(H, now_ns=H + HOUR_NS + GRACE, grace_ns=GRACE, positions=positions, run_ended=False)


def test_not_ready_until_every_stream_has_passed_the_end():
    now = H + 2 * HOUR_NS
    behind = {"deltas": H + HOUR_NS, "trades": H + HOUR_NS - 1}
    assert not hour_ready(H, now_ns=now, grace_ns=GRACE, positions=behind, run_ended=False)
    passed = {"deltas": H + HOUR_NS, "trades": H + HOUR_NS + 5}
    assert hour_ready(H, now_ns=now, grace_ns=GRACE, positions=passed, run_ended=False)


def test_the_last_hour_of_an_ended_run_is_ready():
    now = H + 2 * HOUR_NS
    assert hour_ready(H, now_ns=now, grace_ns=GRACE, positions={"deltas": H + 10}, run_ended=True)


def test_backlog_needs_its_parquet_plus_the_floor():
    assert backlog_bytes_needed(25 * GB, Decimal("0.05"), 8 * GB) == 25 * GB // 20 + 8 * GB


def test_backlog_rounds_its_parquet_up():
    assert backlog_bytes_needed(3, Decimal("0.5"), 0) == 2


DAY = H - 6 * HOUR_NS  # 2026-10-08 00:00:00 UTC


def test_day_of_and_day_name():
    assert day_of(H) == DAY
    assert day_of(DAY + DAY_NS - 1) == DAY
    assert day_name(DAY) == "2026-10-08"
    assert day_name(DAY + DAY_NS) == "2026-10-09"


def test_hours_inside_counts_only_whole_hours():
    # 10:59:50 to 13:00:05: hours 11 and 12 lie wholly inside.
    start = DAY + 11 * HOUR_NS - 10 * SECOND_NS
    end = DAY + 13 * HOUR_NS + 5 * SECOND_NS
    assert hours_inside(start, end) == [DAY + 11 * HOUR_NS, DAY + 12 * HOUR_NS]
    assert hours_inside(DAY + 11 * HOUR_NS, DAY + 12 * HOUR_NS) == [DAY + 11 * HOUR_NS]
    assert hours_inside(start, DAY + 12 * HOUR_NS - 1) == []


def _hours(first, last):
    return set(range(DAY + first * HOUR_NS, DAY + (last + 1) * HOUR_NS, HOUR_NS))


def test_a_day_closes_when_every_hour_of_every_instrument_is_covered():
    assert day_closed(DAY, {"BTC": _hours(0, 23), "ETH": _hours(0, 23)})
    assert not day_closed(DAY, {"BTC": _hours(0, 23), "ETH": _hours(0, 22)})
    assert not day_closed(DAY, {"BTC": _hours(0, 10) | _hours(12, 23)})


def test_hours_before_the_first_recorded_hour_do_not_count():
    assert day_closed(DAY, {"BTC": _hours(6, 23)})
    # An instrument first recorded on a later day does not hold this one back.
    assert day_closed(DAY, {"BTC": _hours(0, 23), "ETH": _hours(30, 40)})
    assert not day_closed(DAY, {"BTC": _hours(30, 40)})
    assert not day_closed(DAY, {})
