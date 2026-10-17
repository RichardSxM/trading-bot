"""covers: spec 0013 AC-4 (``disk_low``) and AC-6 (the stuck entry block, its since time, and the
restart rule).

Pure: no clock, no I/O.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_core.alerts.checks import ENTRY_BLOCK_REASONS, disk_check, entry_block_checks
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Tier
from trading_core.strategy.blocks import BlockReason

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
CONFIG = EngineConfig(env="demo")
GB = 1_000_000_000


def ns(at):
    return int(at.timestamp()) * 1_000_000_000


def block(engine, at, blocked, since):
    """Feed one trader state read; returns every push decided."""
    pushes = []
    for result in entry_block_checks(blocked, {r: ns(s) for r, s in since.items()}, at):
        pushes += engine.check(result).pushes
    return pushes


# disk_low (AC-4) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("free", "ok", "free_gb"),
    [(5 * GB, True, "5.0"), (5 * GB - 1, False, "4.9"), (4_250_000_000, False, "4.2"), (80 * GB, True, "80.0")],
)
def test_disk_check(free, ok, free_gb):
    result = disk_check(free, Decimal(5), NOW)

    assert (result.rule, result.ok, result.fields) == ("disk_low", ok, {"free_gb": free_gb, "floor_gb": "5"})


def test_a_fractional_floor():
    assert not disk_check(2_400_000_000, Decimal("2.5"), NOW).ok
    assert disk_check(2_500_000_000, Decimal("2.5"), NOW).ok


def test_low_disk_is_one_critical_push_that_resolves_with_a_note():
    engine = AlertEngine(CONFIG)

    [push] = engine.check(disk_check(3 * GB, Decimal(5), NOW)).pushes
    assert push.tier is Tier.CRITICAL
    assert push.message == "3.0 GB free on the host root filesystem (the floor is 5 GB)."
    engine.record(push, Delivery.SENT, NOW)

    [note] = engine.check(disk_check(9 * GB, Decimal(5), NOW + timedelta(minutes=10))).pushes
    assert note.tier is Tier.INFO


# entry_block (AC-6) -------------------------------------------------------------------------


def test_the_alerting_set_leaves_out_the_reasons_you_set_or_that_clear_on_a_clock():
    assert {
        BlockReason.HALTED,
        BlockReason.PAUSED,
        BlockReason.DAILY_LOSS_LIMIT,
        BlockReason.TRADE_CAP,
        BlockReason.LOSS_COOLDOWN,
    }.isdisjoint(ENTRY_BLOCK_REASONS)
    assert len(ENTRY_BLOCK_REASONS) == 7


@pytest.mark.parametrize("reason", ["halted", "paused", "daily_loss_limit", "trade_cap", "loss_cooldown", "new_one"])
def test_reasons_outside_the_set_never_alert(reason):
    engine = AlertEngine(CONFIG)

    block(engine, NOW, [reason], {reason: NOW - timedelta(hours=3)})

    assert engine.open_episodes == ()


def test_a_block_pushes_once_it_has_lasted_an_hour_counted_from_its_since():
    engine = AlertEngine(CONFIG)
    since = NOW - timedelta(minutes=50)

    assert block(engine, NOW, ["stale_feed"], {"stale_feed": since}) == []
    [episode] = engine.open_episodes
    assert episode.opened_at == since
    assert episode.fingerprint == "entry_block:stale_feed"
    assert engine.tick(NOW + timedelta(minutes=10) - timedelta(seconds=1)).pushes == ()

    [push] = engine.tick(NOW + timedelta(minutes=10)).pushes
    assert push.tier is Tier.WARNING
    assert push.title == "[demo] Entries blocked for over an hour"
    assert push.message == "No fresh prices from Kraken (stale_feed)."


@pytest.mark.parametrize(
    ("block_s", "title"),
    [(600, "Entries blocked for over 10 min"), (7200, "Entries blocked for over 2 hours")],
)
def test_the_block_title_names_the_configured_length(block_s, title):
    engine = AlertEngine(EngineConfig(env="demo", entry_block_s=block_s))

    [push] = block(engine, NOW, ["stale_feed"], {"stale_feed": NOW - timedelta(hours=3)})

    assert push.title == f"[demo] {title}"


def test_a_block_already_an_hour_old_pushes_at_once():
    engine = AlertEngine(CONFIG)

    [push] = block(engine, NOW, ["exit_failed"], {"exit_failed": NOW - timedelta(hours=2)})

    assert push.episode.fingerprint == "entry_block:exit_failed"


def test_a_block_cleared_before_the_hour_is_stored_and_never_pushed():
    engine = AlertEngine(CONFIG)
    block(engine, NOW, ["stale_feed"], {"stale_feed": NOW})

    assert block(engine, NOW + timedelta(minutes=30), [], {}) == []
    assert engine.open_episodes == ()


def test_the_resolved_note_follows_a_pushed_block():
    engine = AlertEngine(CONFIG)
    [push] = block(engine, NOW, ["loss_unknown"], {"loss_unknown": NOW - timedelta(hours=1)})
    engine.record(push, Delivery.SENT, NOW)

    [note] = block(engine, NOW + timedelta(minutes=5), [], {})

    assert note.tier is Tier.INFO


def test_the_setting_sets_the_hour():
    engine = AlertEngine(EngineConfig(env="demo", entry_block_s=600))

    [push] = block(engine, NOW, ["stale_feed"], {"stale_feed": NOW - timedelta(minutes=10)})

    assert push.episode.rule == "entry_block"


def test_a_reason_back_within_five_minutes_keeps_its_first_start():
    engine = AlertEngine(CONFIG)
    first = NOW - timedelta(minutes=40)
    block(engine, NOW, ["recording_unhealthy"], {"recording_unhealthy": first})
    # The trader restarts: its first state has no reason, then the reason returns with a new since.
    block(engine, NOW + timedelta(minutes=1), [], {})
    back = NOW + timedelta(minutes=3)
    assert block(engine, back, ["recording_unhealthy"], {"recording_unhealthy": back}) == []

    [episode] = engine.open_episodes
    assert episode.opened_at == back
    assert episode.details["since"] == first.isoformat()
    [push] = engine.tick(first + timedelta(hours=1)).pushes
    assert push.episode.fingerprint == "entry_block:recording_unhealthy"


def test_a_reason_back_after_five_minutes_starts_over():
    engine = AlertEngine(CONFIG)
    block(engine, NOW, ["recording_unhealthy"], {"recording_unhealthy": NOW - timedelta(minutes=40)})
    block(engine, NOW + timedelta(minutes=1), [], {})
    back = NOW + timedelta(minutes=7)
    block(engine, back, ["recording_unhealthy"], {"recording_unhealthy": back})

    [episode] = engine.open_episodes
    assert episode.details["since"] == back.isoformat()


def test_a_reason_with_no_since_counts_from_now():
    engine = AlertEngine(CONFIG)

    block(engine, NOW, ["stale_feed"], {})

    [episode] = engine.open_episodes
    assert episode.opened_at == NOW


def test_a_since_in_the_future_counts_from_now():
    engine = AlertEngine(CONFIG)

    block(engine, NOW, ["stale_feed"], {"stale_feed": NOW + timedelta(minutes=5)})

    [episode] = engine.open_episodes
    assert episode.opened_at == NOW
