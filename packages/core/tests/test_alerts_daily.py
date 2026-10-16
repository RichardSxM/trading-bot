"""covers: spec 0013 AC-18 (the daily note: when it is due, its fingerprint, down minutes, its text, and
its push never held, muted, capped, or counted, also on a retry).

Pure: no clock, no I/O.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trading_core.alerts import daily
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Hold, HoldKind, Observation, PushReason, RuleKind, Tier
from trading_core.alerts.rules import BY_ID, DAILY

AMS = "Europe/Amsterdam"
DAY = date(2026, 10, 17)


def figures(**overrides):
    base = daily.DailyFigures(opened={}, still_open=0, failed=0, failed_titles=(), down_s=0, trades=0)
    return replace(base, **overrides)


def engine_error(at, service):
    return Observation(f"log:{service}", service, at, None, None, {}, text="2026 [ERROR] TRADER-001.DataEngine: boom")


# When it is due ------------------------------------------------------------------------------


def test_due_from_eight_amsterdam_in_summer_time():
    # 2026-07-01: Amsterdam is UTC+2, so 08:00 local is 06:00 UTC.
    assert daily.due(datetime(2026, 7, 1, 5, 59, tzinfo=UTC), "08:00", AMS) is None
    assert daily.due(datetime(2026, 7, 1, 6, 0, tzinfo=UTC), "08:00", AMS) == date(2026, 7, 1)


def test_due_from_eight_amsterdam_in_winter_time():
    # 2026-12-01: Amsterdam is UTC+1, so 08:00 local is 07:00 UTC.
    assert daily.due(datetime(2026, 12, 1, 6, 59, tzinfo=UTC), "08:00", AMS) is None
    assert daily.due(datetime(2026, 12, 1, 7, 0, tzinfo=UTC), "08:00", AMS) == date(2026, 12, 1)


def test_due_takes_the_local_date_not_the_utc_one():
    # 23:30 UTC on 1 July is 01:30 on 2 July in Amsterdam: before 08:00, so nothing is due.
    assert daily.due(datetime(2026, 7, 1, 23, 30, tzinfo=UTC), "08:00", AMS) is None
    # 21:30 UTC is 23:30 local, still the 1st: the catch up for the 1st is still due.
    assert daily.due(datetime(2026, 7, 1, 21, 30, tzinfo=UTC), "08:00", AMS) == date(2026, 7, 1)


def test_fingerprint_is_daily_and_the_local_date():
    assert daily.fingerprint(DAY) == "daily:2026-10-17"


def test_the_daily_rule_is_an_internal_info_rule():
    assert BY_ID["daily"] is DAILY
    assert (DAILY.tier, DAILY.kind, DAILY.source, DAILY.codes) == (Tier.INFO, RuleKind.INTERNAL, "self", frozenset())


# What it counts ------------------------------------------------------------------------------


def test_down_seconds_clips_each_episode_to_the_last_24_hours():
    now = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
    spans = [
        (now - timedelta(hours=30), now - timedelta(hours=23, minutes=50)),  # 10 min inside
        (now - timedelta(hours=2), now - timedelta(hours=1, minutes=55)),  # 5 min
        (now - timedelta(minutes=3), None),  # still open: 3 min
        (now - timedelta(hours=40), now - timedelta(hours=30)),  # outside
    ]
    assert daily.down_seconds(spans, now) == 18 * 60


def test_yesterday_is_the_utc_day_before():
    start, end = daily.yesterday(datetime(2026, 10, 17, 6, 0, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 10, 16, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC))


def test_text_reports_every_figure():
    title, body = daily.text(
        DAY,
        figures(
            opened={Tier.CRITICAL: 1, Tier.INFO: 4},
            still_open=2,
            failed=7,
            failed_titles=("[live] A", "[live] B"),
            down_s=185,
            trades=3,
            pnl={"USD": Decimal("12.34")},
            trade_day=date(2026, 10, 16),
            backup="healthy",
        ),
    )
    assert title == "Daily note 2026-10-17"
    assert body.splitlines() == [
        "Last 24 h: 1 critical, 0 warning, 4 info opened. Still open: 2. Failed pushes: 7.",
        "Failed: [live] A; [live] B; and 5 more.",
        "Trader down: 3 min in the last 24 h.",
        "Yesterday (UTC 2026-10-16): 3 trades, realized P&L +12.34 USD.",
        "Backup: healthy.",
    ]


def test_text_says_no_trades_and_shows_a_loss_unsigned():
    _, quiet = daily.text(DAY, figures())
    assert "Yesterday: no trades." in quiet
    assert "Failed:" not in quiet
    _, loss = daily.text(DAY, figures(trades=1, pnl={"USD": Decimal("-0.50")}))
    assert "1 trade, realized P&L -0.50 USD." in loss


# The engine ----------------------------------------------------------------------------------


NOW = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


def test_the_note_is_stored_resolved_with_its_fingerprint_and_pushed_silently():
    engine = AlertEngine(EngineConfig(env="live"))

    decision = engine.daily_note(DAY, figures(), NOW)

    (episode,) = decision.store
    (push,) = decision.pushes
    assert (episode.rule, episode.fingerprint, episode.severity, episode.source) == (
        "daily",
        "daily:2026-10-17",
        Tier.INFO,
        "self",
    )
    assert episode.resolved_at == NOW
    assert not episode.is_open
    assert episode.title == "[live] Daily note 2026-10-17"
    assert (push.reason, push.tier) == (PushReason.DAILY, Tier.INFO)
    assert engine.open_episodes == ()


def test_the_note_is_never_muted_or_held_by_a_deploy():
    for kind in (HoldKind.MUTE, HoldKind.DEPLOY):
        engine = AlertEngine(EngineConfig(env="demo"))
        engine.open_hold(Hold(kind, "w", NOW, NOW + timedelta(hours=1)))

        decision = engine.daily_note(DAY, figures(), NOW)

        assert [p.reason for p in decision.pushes] == [PushReason.DAILY]
        assert decision.store[0].delivery is Delivery.PENDING


def test_the_note_passes_the_cap_and_takes_no_slot():
    # A cap of 3 sends two warning or info pushes an hour; the third slot is the storm push.
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    first = engine.observe(engine_error(NOW, "trader"))
    for push in first.pushes:
        engine.record(push, Delivery.SENT, NOW)

    note = engine.daily_note(DAY, figures(), NOW)
    for push in note.pushes:
        engine.record(push, Delivery.SENT, NOW)
    second = engine.observe(engine_error(NOW, "recorder"))

    assert [p.reason for p in note.pushes] == [PushReason.DAILY]
    # Had the note counted, this warning would be capped and replaced by the storm push.
    assert [p.episode.fingerprint for p in second.pushes] == ["engine_error:recorder"]


def test_a_failed_note_retries_as_a_daily_push_through_a_mute():
    engine = AlertEngine(EngineConfig(env="demo"))
    (push,) = engine.daily_note(DAY, figures(), NOW).pushes
    engine.record(push, Delivery.FAILED, NOW)
    engine.open_hold(Hold(HoldKind.MUTE, "m", NOW, NOW + timedelta(hours=4)))

    retried = engine.retry(NOW + timedelta(minutes=1))

    assert [(p.reason, p.episode.fingerprint) for p in retried.pushes] == [(PushReason.DAILY, "daily:2026-10-17")]
