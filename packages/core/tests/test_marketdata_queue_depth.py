"""covers: spec 0007 AC-13 (queue depth warning levels, the 60 s throttle, the stop check)."""

from trading_core.marketdata.queue_depth import (
    QUEUE_FULL,
    QUEUE_HIGH,
    STOP_QUEUE,
    STOP_QUEUE_FULL,
    queue_alert,
    stop_code,
)

CAP = 100_000
S = 1_000_000_000


def _alert(depth, now_ns=0, last=None):
    return queue_alert(depth, CAP, warn_pct=50, now_ns=now_ns, last_logged=last or {})


def test_below_the_warn_level_logs_nothing():
    assert _alert(49_999) is None
    assert _alert(0) is None


def test_at_the_warn_level_logs_high_and_at_capacity_logs_full():
    assert _alert(50_000) == QUEUE_HIGH
    assert _alert(99_999) == QUEUE_HIGH
    assert _alert(CAP) == QUEUE_FULL


def test_each_code_repeats_only_after_60_s():
    last = {QUEUE_HIGH: 10 * S}
    assert _alert(60_000, now_ns=69 * S, last=last) is None
    assert _alert(60_000, now_ns=70 * S, last=last) == QUEUE_HIGH
    # A different code is throttled on its own.
    assert _alert(CAP, now_ns=11 * S, last=last) == QUEUE_FULL


def test_the_stop_check_flags_a_queue_at_99_percent():
    assert stop_code(99_000, CAP) == STOP_QUEUE_FULL
    assert stop_code(CAP, CAP) == STOP_QUEUE_FULL
    assert stop_code(98_999, CAP) == STOP_QUEUE
    assert stop_code(10_000, CAP) == STOP_QUEUE
