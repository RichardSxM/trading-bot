"""covers: spec 0007 AC-2 (hour bounds for file names) and the ready rule of *Lifecycle of one hour*."""

from trading_core.marketdata.hours import HOUR_NS, SECOND_NS, hour_last_ns, hour_of, hour_ready

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
