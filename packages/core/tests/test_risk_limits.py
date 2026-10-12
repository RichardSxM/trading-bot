"""covers: spec 0010 AC-8 (what counts, once per (position_ref, opened_ns)), AC-9 (daily trip and its
block until midnight), AC-10 (total trip wins), AC-11 (trade cap), AC-12 (cooldown), AC-14 (the roll).

Pure: no clock, no I/O.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_core.control.halt import HaltReason
from trading_core.risk.limits import (
    LossLimits,
    LossTally,
    evaluate,
    on_closed,
    on_opened,
    roll,
    tripped,
    utc_day,
)
from trading_core.strategy.blocks import BlockReason

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
LIMITS = LossLimits(
    daily_usd=Decimal(20), total_usd=Decimal(100), total_since=date(2026, 10, 1), max_trades_per_day=3, cooldown_s=600
)


def _ns(at):
    return int(at.timestamp()) * 1_000_000_000


def _key(at=NOW, ref="PF_XBTUSD.KRAKEN-PlaceholderTimed-000"):
    return (ref, _ns(at))


def _tally(**kwargs):
    return LossTally(day=NOW.date(), **kwargs)


def _closed(tally, pnl, at=NOW, key=None):
    return on_closed(tally, key or _key(at - timedelta(minutes=10)), Decimal(pnl), at, LIMITS)


def test_a_profit_is_a_negative_loss_and_nothing_trips():
    verdict = evaluate(_closed(_tally(), "5"), Decimal("1.5"), LIMITS, NOW)

    assert (verdict.loss_today, verdict.loss_total) == (Decimal("-6.5"), Decimal("-6.5"))
    assert verdict.trip is None
    assert verdict.blocks == frozenset()


def test_realized_and_unrealized_add_up_and_trip_daily_exactly_at_the_limit():
    tally = _closed(_tally(), "-15", at=NOW - timedelta(hours=2))

    under = evaluate(tally, Decimal("-4.99"), LIMITS, NOW)
    at = evaluate(tally, Decimal("-5"), LIMITS, NOW)

    assert (under.trip, under.loss_today) == (None, Decimal("19.99"))
    assert at.trip is HaltReason.DAILY_LOSS_LIMIT
    assert BlockReason.DAILY_LOSS_LIMIT in at.blocks


def test_after_a_trip_the_block_holds_even_when_the_loss_shrinks_and_no_second_trip_fires():
    tally = tripped(_closed(_tally(), "-25", at=NOW - timedelta(hours=2)), HaltReason.DAILY_LOSS_LIMIT)

    still_over = evaluate(tally, Decimal(0), LIMITS, NOW)
    recovered = evaluate(tally, Decimal(30), LIMITS, NOW)

    assert still_over.trip is None
    assert recovered.trip is None
    assert BlockReason.DAILY_LOSS_LIMIT in recovered.blocks


def test_the_midnight_roll_clears_the_daily_block_and_today_but_keeps_the_total():
    tally = tripped(_closed(_tally(), "-25", at=NOW - timedelta(hours=2)), HaltReason.DAILY_LOSS_LIMIT)
    midnight = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)

    rolled = roll(tally, midnight)
    verdict = evaluate(rolled, Decimal(0), LIMITS, midnight)

    assert rolled.day == date(2026, 10, 13)
    assert (rolled.realized_today, rolled.realized_total) == (Decimal(0), Decimal(-25))
    assert not rolled.daily_tripped
    assert BlockReason.DAILY_LOSS_LIMIT not in verdict.blocks
    assert roll(rolled, midnight + timedelta(hours=1)) is rolled  # same day: unchanged


def test_the_midnight_roll_resets_the_trade_count_and_lifts_the_trade_cap():
    """covers: spec 0010 AC-11 (blocked until the next UTC midnight), AC-14 (the roll)"""
    tally = _tally()
    for minutes in (10, 30, 50):
        tally = on_opened(tally, _key(NOW - timedelta(minutes=minutes)))
    midnight = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)
    assert BlockReason.TRADE_CAP in evaluate(tally, Decimal(0), LIMITS, NOW).blocks

    rolled = roll(tally, midnight)
    verdict = evaluate(rolled, Decimal(0), LIMITS, midnight)

    assert rolled.trades_today == 0
    assert BlockReason.TRADE_CAP not in verdict.blocks
    assert on_opened(rolled, _key(midnight)).trades_today == 1  # the new day counts afresh


def test_when_both_limits_trip_at_once_total_wins():
    tally = _closed(_tally(), "-150", at=NOW - timedelta(hours=1))

    verdict = evaluate(tally, Decimal(0), LIMITS, NOW)

    assert verdict.trip is HaltReason.TOTAL_LOSS_LIMIT


def test_after_the_total_trips_the_daily_trips_on_the_next_check():
    tally = tripped(_closed(_tally(), "-150", at=NOW - timedelta(hours=1)), HaltReason.TOTAL_LOSS_LIMIT)

    verdict = evaluate(tally, Decimal(0), LIMITS, NOW)

    assert verdict.trip is HaltReason.DAILY_LOSS_LIMIT
    assert tripped(tripped(tally, HaltReason.DAILY_LOSS_LIMIT), HaltReason.TOTAL_LOSS_LIMIT).total_tripped


def test_the_total_counts_earlier_days_since_total_since_only():
    yesterday = NOW - timedelta(days=1)
    before_since = datetime(2026, 9, 30, 12, tzinfo=UTC)
    tally = _closed(_tally(), "-30", at=yesterday)
    tally = _closed(tally, "-500", at=before_since)

    verdict = evaluate(tally, Decimal(0), LIMITS, NOW)

    assert (verdict.loss_today, verdict.loss_total) == (Decimal(0), Decimal(30))


def test_a_key_counted_twice_counts_once():
    key = _key(NOW - timedelta(minutes=30))
    tally = on_opened(on_opened(_tally(), key), key)
    tally = _closed(_closed(tally, "-3", key=key), "-3", key=key)

    assert tally.trades_today == 1
    assert tally.realized_today == Decimal(-3)


def test_three_round_trips_on_the_same_position_ref_count_three_times():
    """NETTING reuses the position id; ``opened_ns`` tells the round trips apart (AC-8, AC-11)."""
    tally = _tally()
    for minutes in (10, 30, 50):
        opened = NOW - timedelta(minutes=minutes + 5)
        key = _key(opened)
        tally = _closed(on_opened(tally, key), "-1", at=opened + timedelta(minutes=5), key=key)

    verdict = evaluate(tally, Decimal(0), LIMITS, NOW)

    assert (tally.trades_today, tally.realized_today) == (3, Decimal(-3))
    assert BlockReason.TRADE_CAP in verdict.blocks


def test_a_position_opened_yesterday_does_not_count_toward_today_s_trades():
    tally = on_opened(_tally(), _key(NOW - timedelta(days=1)))

    assert tally.trades_today == 0


def test_the_cooldown_runs_from_the_newest_losing_close_and_a_winner_does_not_move_it():
    first = NOW - timedelta(minutes=8)
    later = NOW - timedelta(minutes=2)
    tally = _closed(_tally(), "-1", at=first)
    tally = _closed(tally, "-1", at=later)
    tally = _closed(tally, "4", at=NOW - timedelta(minutes=1))

    verdict = evaluate(tally, Decimal(0), LIMITS, NOW)
    after = evaluate(tally, Decimal(0), LIMITS, later + timedelta(seconds=600))

    assert verdict.cooldown_until == later + timedelta(seconds=600)
    assert BlockReason.LOSS_COOLDOWN in verdict.blocks
    assert after.cooldown_until is None
    assert BlockReason.LOSS_COOLDOWN not in after.blocks


def test_cooldown_zero_turns_it_off():
    tally = _closed(_tally(), "-1", at=NOW - timedelta(seconds=1))
    off = LossLimits(
        daily_usd=Decimal(20), total_usd=Decimal(100), total_since=date(2026, 10, 1), max_trades_per_day=3, cooldown_s=0
    )

    verdict = evaluate(tally, Decimal(0), off, NOW)

    assert verdict.cooldown_until is None
    assert BlockReason.LOSS_COOLDOWN not in verdict.blocks


def test_only_loss_limits_can_be_tripped_and_times_must_be_aware():
    with pytest.raises(ValueError, match="not a loss limit"):
        tripped(_tally(), HaltReason.MANUAL_STOP)
    with pytest.raises(ValueError, match="naive"):
        utc_day(datetime(2026, 10, 12))  # noqa: DTZ001  # the naive datetime is the point
