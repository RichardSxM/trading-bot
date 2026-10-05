"""covers: spec 0006 AC-3 (boundary and side), AC-6 (stop price), AC-18 (block reasons), AC-19 (exit reason)."""

import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_core.recording.enums import Intent, OrderSide, OrderType, PositionSide
from trading_core.strategy.blocks import PLAIN_WORDS, BlockReason, EntryBlocks, entries_blocked
from trading_core.strategy.placeholder import ExitReason, entry_side, exit_reason, next_boundary_ns, stop_price


def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) * 1_000_000_000


@pytest.mark.parametrize(
    ("now", "boundary"),
    [
        ("2026-10-04 13:00:00", "2026-10-04 13:15:00"),
        ("2026-10-04 13:14:59", "2026-10-04 13:15:00"),
        ("2026-10-04 13:15:01", "2026-10-04 13:30:00"),
        ("2026-10-04 23:50:00", "2026-10-05 00:00:00"),
    ],
)
def test_next_boundary_is_the_next_quarter_hour_utc(now, boundary):
    assert next_boundary_ns(ns(now), 15) == ns(boundary)


def test_next_boundary_follows_the_interval():
    assert next_boundary_ns(ns("2026-10-04 13:07:00"), 5) == ns("2026-10-04 13:10:00")


def test_side_is_buy_when_the_newest_close_is_at_or_above_the_lookback_close():
    closes = [Decimal(x) for x in (100, 90, 95, 99, 98, 100)]
    assert entry_side(closes, 5) == OrderSide.BUY
    assert entry_side([*closes[:-1], Decimal("99.5")], 5) == OrderSide.SELL


def test_side_uses_only_the_newest_lookback_plus_one_closes():
    closes = [Decimal(x) for x in (200, 100, 100, 100, 100, 100, 101)]
    assert entry_side(closes, 5) == OrderSide.BUY


def test_side_is_none_during_warmup():
    assert entry_side([Decimal(1)] * 5, 5) is None


def test_stop_is_below_a_long_and_above_a_short_rounded_away_from_the_market():
    assert stop_price(Decimal(85250), PositionSide.LONG, Decimal("0.3"), Decimal(1)) == Decimal(84994)  # 84994.25
    assert stop_price(Decimal(85250), PositionSide.SHORT, Decimal("0.3"), Decimal(1)) == Decimal(85506)  # 85505.75
    assert stop_price(Decimal("100.03"), PositionSide.LONG, Decimal(1), Decimal("0.05")) == Decimal("99.00")


def test_stop_refuses_a_flat_position():
    with pytest.raises(ValueError, match="flat position has no stop"):
        stop_price(Decimal(1), PositionSide.FLAT, Decimal(1), Decimal(1))


@pytest.mark.parametrize(
    ("abandoned", "order_type", "intent", "reason"),
    [
        (True, OrderType.STOP_MARKET, Intent.EXIT, ExitReason.ABANDONED),
        (False, OrderType.STOP_MARKET, Intent.ENTER, ExitReason.STOP),
        (False, OrderType.MARKET, Intent.EXIT, ExitReason.TIME),
        (False, OrderType.MARKET, Intent.ENTER, ExitReason.STOP_FAILURE),
    ],
)
def test_exit_reason(abandoned, order_type, intent, reason):
    assert exit_reason(abandoned, order_type, intent) == reason


def test_blocks_add_clear_and_list_sorted():
    blocks = EntryBlocks()
    assert blocks.add(BlockReason.STALE_FEED)
    assert not blocks.add(BlockReason.STALE_FEED)
    blocks.add(BlockReason.EXIT_FAILED)

    assert blocks.active() == (BlockReason.EXIT_FAILED, BlockReason.STALE_FEED)
    assert blocks.clear(BlockReason.STALE_FEED)
    assert not blocks.clear(BlockReason.STALE_FEED)


def test_a_restart_only_block_cannot_be_cleared():
    blocks = EntryBlocks()
    blocks.add(BlockReason.RECORD_CONFLICT)
    with pytest.raises(ValueError, match="holds until restart"):
        blocks.clear(BlockReason.RECORD_CONFLICT)
    assert blocks.active() == (BlockReason.RECORD_CONFLICT,)


def test_blocks_survive_concurrent_writers():
    blocks = EntryBlocks()
    threads = [threading.Thread(target=blocks.add, args=(reason,)) for reason in BlockReason for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(blocks.active()) == set(BlockReason)


def test_entries_blocked_adds_paused():
    blocks = EntryBlocks()
    blocks.add(BlockReason.STALE_FEED)
    assert entries_blocked(blocks, paused=True) == ["paused", "stale_feed"]
    assert entries_blocked(EntryBlocks(), paused=False) == []


def test_every_reason_has_plain_words():
    assert set(PLAIN_WORDS) == set(BlockReason)
