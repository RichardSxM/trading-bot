"""covers: spec 0007 AC-2 (hour bounds), the ready rule of *Lifecycle of one hour*, AC-9 (backlog space),
AC-5 (day close), AC-12 (watermark, late rows, cause, row cap)."""

from decimal import Decimal

from trading_core.marketdata.hours import (
    DAY_NS,
    GB,
    HOUR_NS,
    SECOND_NS,
    WatermarkState,
    backlog_bytes_needed,
    cap_watermark,
    day_closed,
    day_name,
    day_of,
    end_run,
    hour_last_ns,
    hour_of,
    hour_ready,
    hours_inside,
    scan_rows,
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
    watermark = H + HOUR_NS
    assert not hour_ready(H, now_ns=H + HOUR_NS + GRACE - 1, grace_ns=GRACE, watermark=watermark, run_ended=False)
    assert hour_ready(H, now_ns=H + HOUR_NS + GRACE, grace_ns=GRACE, watermark=watermark, run_ended=False)


def test_not_ready_until_the_watermark_has_passed_the_end():
    now = H + 2 * HOUR_NS
    assert not hour_ready(H, now_ns=now, grace_ns=GRACE, watermark=H + HOUR_NS - 1, run_ended=False)
    assert not hour_ready(H, now_ns=now, grace_ns=GRACE, watermark=None, run_ended=False)
    assert hour_ready(H, now_ns=now, grace_ns=GRACE, watermark=H + HOUR_NS, run_ended=False)


def test_the_last_hour_of_an_ended_run_is_ready():
    now = H + 2 * HOUR_NS
    assert hour_ready(H, now_ns=now, grace_ns=GRACE, watermark=H + 10, run_ended=True)


WINDOW = 600 * SECOND_NS


def _scan(ts, run="a", state=None, window=WINDOW):
    return scan_rows([H + s * SECOND_NS for s in ts], run, state or WatermarkState(), window_ns=window)


def test_rows_inside_the_window_are_not_late_and_the_watermark_trails_the_newest():
    scan = _scan([0, 700, 200, 101])
    assert scan.causes == [None, None, None, None]
    assert scan.lateness_ns == [0, 0, 500 * SECOND_NS, 599 * SECOND_NS]
    assert scan.state == WatermarkState(H + 100 * SECOND_NS, H + 700 * SECOND_NS, "a")


def test_a_row_below_the_watermark_is_late_and_the_watermark_never_goes_down():
    scan = _scan([0, 700, 99, 100, 650])
    assert scan.causes == [None, None, "window", None, None]
    assert scan.lateness_ns[2] == 601 * SECOND_NS
    assert scan.state.watermark == H + 100 * SECOND_NS


def test_a_row_of_a_newer_run_below_the_watermark_is_a_run_overlap():
    first = _scan([0, 700])
    second = _scan([50, 800, 150], run="b", state=first.state)
    assert second.causes == ["run_overlap", None, "window"]


def test_window_zero_makes_every_backwards_row_late():
    scan = _scan([0, 5, 5, 4, 6], window=0)
    assert scan.causes == [None, None, None, "window", None]


def test_chunking_never_changes_the_classification():
    ts = [0, 900, 400, 299, 300, 1_500, 905, 1_000]
    whole = _scan(ts)
    state, causes = WatermarkState(), []
    for start in range(0, len(ts), 3):
        part = _scan(ts[start : start + 3], state=state)
        causes += part.causes
        state = part.state
    assert causes == whole.causes
    assert state == whole.state


def test_end_run_raises_the_watermark_to_the_newest_row():
    state = _scan([0, 700]).state
    assert end_run(state) == WatermarkState(H + 700 * SECOND_NS, H + 700 * SECOND_NS, "a")
    assert end_run(WatermarkState()) == WatermarkState()


def test_the_cap_raises_the_watermark_until_at_most_max_rows_are_held():
    held = [10, 11, 11, 12, 13]
    assert cap_watermark(held, 5, 5, max_rows=5) == 5
    assert cap_watermark(held, 5, 5, max_rows=4) == 10
    assert cap_watermark(held, 5, 5, max_rows=3) == 11  # both rows at 11 go together
    assert cap_watermark(held, 5, None, max_rows=1) == 12
    # Only the oldest excess rows need to be passed.
    assert cap_watermark(held[:2], 5, 5, max_rows=3) == 11


def test_watermark_state_round_trips():
    state = WatermarkState(1, 2, "run")
    assert WatermarkState.from_dict(state.to_dict()) == state
    assert WatermarkState.from_dict({}) == WatermarkState()


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
