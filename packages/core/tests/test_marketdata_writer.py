"""covers: spec 0012 AC-2 (the flush cap), AC-3 (the boundary without drift), AC-6 (busy and slow flush rules)."""

from trading_core.marketdata.writer import (
    busy_pct,
    busy_warn,
    next_boundary_ns,
    rotation_due,
    should_flush,
    slow_flush_due,
)

S = 1_000_000_000
HOUR = 3_600 * S
MS = 1_000_000
# 2026-10-08 06:00:00 UTC
H = 1_791_439_200 * S


def test_boundary_is_the_next_whole_hour_strictly_after_now():
    assert next_boundary_ns(H - 10 * S) == H
    assert next_boundary_ns(H) == H + HOUR
    # A rotating write 7 s late still gives the next whole hour, not now + 1 h.
    assert next_boundary_ns(H + 7 * S) == H + HOUR


def test_rotation_is_due_at_or_after_the_boundary():
    assert not rotation_due(H - 1, H)
    assert rotation_due(H, H)
    assert rotation_due(H + S, H)


def test_flush_at_the_cap():
    assert not should_flush(4_095, 4_096)
    assert should_flush(4_096, 4_096)


def test_busy_pct_and_the_warn_level():
    assert busy_pct(15 * S, 60 * S) == 25
    assert busy_warn(25)
    assert not busy_warn(24)
    assert busy_pct(5 * S, 0) == 0


def test_slow_flush_logs_once_a_minute():
    assert not slow_flush_due(250 * MS, now_ns=0, last_logged_ns=None)
    assert slow_flush_due(300 * MS, now_ns=0, last_logged_ns=None)
    assert not slow_flush_due(300 * MS, now_ns=59 * S, last_logged_ns=0)
    assert slow_flush_due(300 * MS, now_ns=60 * S, last_logged_ns=0)
