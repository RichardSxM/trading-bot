"""covers: spec 0013 AC-3 and AC-7 (log lines become episodes), AC-4 (service, container, and
storage conditions), AC-9 (repeats bump, pairs resolve), and AC-10 (the hourly storm cap).

Pure: no clock, no I/O.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import (
    CheckResult,
    Decision,
    Delivery,
    Episode,
    Hold,
    HoldKind,
    Observation,
    Push,
    PushReason,
    Tier,
)

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
CONFIG = EngineConfig(env="demo")


def line(service, code, level="ERROR", at=NOW, **fields):
    return Observation(f"log:{service}", service, at, code, level, {"code": code, "msg": "something", **fields})


def sent(engine, decision, at, delivery=Delivery.SENT):
    return [stored for push in decision.pushes for stored in engine.record(push, delivery, at)]


def test_a_critical_risk_line_pushes_once_with_its_event_key():
    engine = AlertEngine(CONFIG)

    decision = engine.observe(line("trader", "order_rejected", "CRITICAL", event_key="stop_not_accepted:S1"))

    (episode,) = decision.store
    (push,) = decision.pushes
    assert (episode.fingerprint, episode.severity, episode.source) == (
        "stop_not_accepted:S1",
        Tier.CRITICAL,
        "log:trader",
    )
    assert episode.title == "[demo] Stop not accepted"
    assert "stop_not_accepted:S1" in episode.message
    assert push.tier is Tier.CRITICAL


def test_a_repeat_bumps_and_pushes_nothing():
    engine = AlertEngine(CONFIG)
    sent(engine, engine.observe(line("trader", "kraken_auth_failed", "CRITICAL")), NOW)

    later = NOW + timedelta(seconds=30)
    decision = engine.observe(line("trader", "kraken_auth_failed", "CRITICAL", at=later))

    assert decision.pushes == ()
    (bumped,) = decision.store
    assert (bumped.occurrences, bumped.last_seen_at) == (2, later)


def test_a_loss_limit_halt_and_its_halt_event_make_one_push():
    engine = AlertEngine(CONFIG)

    first = engine.observe(line("trader", "loss_limit_halt", "CRITICAL", halt_id="h1"))
    second = engine.observe(line("trader", "halt", "CRITICAL", event_key="halt:h1"))

    assert len(first.pushes) == 1
    assert second.pushes == ()
    assert second.store[0].occurrences == 2


def test_an_event_closes_quietly_and_a_later_line_opens_a_new_episode():
    engine = AlertEngine(CONFIG)
    sent(engine, engine.observe(line("recorder", "recorder_write_failed", "CRITICAL")), NOW)

    closed = engine.tick(NOW + timedelta(minutes=15))
    reopened = engine.observe(line("recorder", "recorder_write_failed", "CRITICAL", at=NOW + timedelta(minutes=16)))

    assert closed.pushes == ()
    assert closed.store[0].resolved_at is not None
    assert len(reopened.pushes) == 1


def test_a_pair_resolves_on_its_partner_from_the_same_service():
    engine = AlertEngine(CONFIG)
    sent(engine, engine.observe(line("recorder", "catalog_disk_full", "CRITICAL")), NOW)

    other = engine.observe(line("compactor", "catalog_disk_ok", "INFO", at=NOW + timedelta(minutes=1)))
    same = engine.observe(line("recorder", "catalog_disk_ok", "INFO", at=NOW + timedelta(minutes=2)))

    assert other == Decision()
    (resolved,) = same.store
    assert resolved.resolved_at == NOW + timedelta(minutes=2)
    (note,) = same.pushes
    assert (note.reason, note.tier) == (PushReason.RESOLVED, Tier.INFO)


def test_a_pair_waits_its_min_open_and_resolves_on_restart():
    engine = AlertEngine(CONFIG)
    opened = engine.observe(line("trader", "context_feed_down", "WARNING"))
    assert opened.pushes == ()  # held for 300 s

    released = engine.tick(NOW + timedelta(seconds=300))
    sent(engine, released, NOW + timedelta(seconds=300))
    restarted = engine.service_started("trader", NOW + timedelta(minutes=10))

    assert len(released.pushes) == 1
    assert restarted.store[0].resolved_at == NOW + timedelta(minutes=10)
    assert [p.reason for p in restarted.pushes] == [PushReason.RESOLVED]


def test_a_pair_shorter_than_its_min_open_is_never_pushed():
    engine = AlertEngine(CONFIG)
    engine.observe(line("trader", "context_feed_down", "WARNING"))

    up = engine.observe(line("trader", "context_feed_up", "INFO", at=NOW + timedelta(seconds=60)))

    assert up.pushes == ()
    assert up.store[0].resolved_at is not None


def test_a_restart_older_than_the_episode_resolves_nothing():
    engine = AlertEngine(CONFIG)
    engine.observe(line("recorder", "catalog_disk_full", "CRITICAL"))

    assert engine.service_started("recorder", NOW - timedelta(hours=1)).store == ()


def test_a_quiet_pair_resolves_after_24_hours():
    engine = AlertEngine(CONFIG)
    sent(engine, engine.observe(line("recorder", "catalog_disk_full", "CRITICAL")), NOW)

    assert engine.tick(NOW + timedelta(hours=23)).store == ()
    later = engine.tick(NOW + timedelta(hours=24))

    assert later.store[0].resolved_at == NOW + timedelta(hours=24)


def test_five_login_failures_in_ten_minutes_open_one_burst():
    engine = AlertEngine(CONFIG)
    decisions = [
        engine.observe(line("dashboard", "login_failed", "WARNING", at=NOW + timedelta(minutes=2 * i), client=f"c{i}"))
        for i in range(6)
    ]

    assert [len(d.pushes) for d in decisions] == [0, 0, 0, 0, 1, 0]
    burst = decisions[4].store[0]
    assert (burst.fingerprint, burst.severity) == ("login_failed_burst", Tier.WARNING)
    assert decisions[5].store[0].occurrences == 2


def test_login_failures_spread_over_more_than_ten_minutes_open_nothing():
    engine = AlertEngine(CONFIG)
    decisions = [
        engine.observe(line("dashboard", "login_failed", "WARNING", at=NOW + timedelta(minutes=3 * i)))
        for i in range(8)
    ]

    assert all(d.pushes == () for d in decisions)


def test_login_failures_from_another_service_never_burst():
    # covers: AC-7, the burst counts the dashboard's login lines only
    engine = AlertEngine(CONFIG)
    decisions = [
        engine.observe(line("trader", "login_failed", "WARNING", at=NOW + timedelta(seconds=10 * i))) for i in range(6)
    ]

    assert all(d.pushes == () for d in decisions)
    assert all(e.rule != "login_failed_burst" for d in decisions for e in d.store)


def test_five_login_failures_exactly_ten_minutes_apart_still_burst():
    # covers: AC-7, the window is "within any 10 min", so a line exactly 600 s old still counts
    engine = AlertEngine(CONFIG)
    decisions = [
        engine.observe(line("dashboard", "login_failed", "WARNING", at=NOW + timedelta(seconds=150 * i)))
        for i in range(5)
    ]

    assert [len(d.pushes) for d in decisions] == [0, 0, 0, 0, 1]


def test_service_down_waits_two_minutes():
    engine = AlertEngine(CONFIG)
    down = CheckResult("service_down", ok=False, at=NOW, subject="clock", fields={"state": "exited"})

    opened = engine.check(down)
    early = engine.tick(NOW + timedelta(seconds=119))
    released = engine.tick(NOW + timedelta(seconds=120))

    assert opened.pushes == ()
    assert early.pushes == ()
    (push,) = released.pushes
    assert push.episode.fingerprint == "service_down:clock"
    assert push.title == "[demo] Service down: clock"


def test_postgres_unreachable_for_less_than_a_minute_is_never_pushed():
    engine = AlertEngine(CONFIG)
    engine.check(CheckResult("postgres_unreachable", ok=False, at=NOW))

    back = engine.check(CheckResult("postgres_unreachable", ok=True, at=NOW + timedelta(seconds=30)))

    assert back.pushes == ()
    assert back.store[0].resolved_at is not None


def test_redis_unreachable_pushes_critical_after_a_minute():
    engine = AlertEngine(CONFIG)
    engine.check(CheckResult("redis_unreachable", ok=False, at=NOW))

    (push,) = engine.tick(NOW + timedelta(seconds=60)).pushes

    assert push.tier is Tier.CRITICAL


# AC-10 ----------------------------------------------------------------------------------------


def test_the_storm_cap_holds_the_hour_after_29_pushes():
    engine = AlertEngine(CONFIG)
    pushes: list[Push] = []
    stored: list[Episode] = []
    for i in range(40):
        decision = engine.observe(line("recorder", "book_gap", "WARNING", kind=f"k{i}", duration_s=90))
        stored += decision.store
        for push in decision.pushes:
            engine.record(push, Delivery.SENT, NOW)
            pushes.append(push)

    storm = [p for p in pushes if p.episode.rule == "alert_storm"]
    assert len(pushes) == 30
    assert len(storm) == 1
    assert storm[0].tier is Tier.WARNING
    assert storm[0].message.startswith("alert storm: 1 alerts held")
    capped = {e.fingerprint for e in stored if e.delivery is Delivery.CAPPED}
    assert len(capped) == 11  # the one the storm replaced, then ten more

    critical = engine.observe(line("recorder", "recorder_write_failed", "CRITICAL"))
    assert len(critical.pushes) == 1


def test_the_storm_episode_counts_what_it_held():
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    stored: list[Episode] = []
    for i in range(5):
        decision = engine.observe(line("recorder", "book_gap", "WARNING", kind=f"k{i}", duration_s=90))
        stored += decision.store
        sent(engine, decision, NOW)

    storms = [e for e in stored if e.rule == "alert_storm"]
    assert storms[-1].occurrences == 3


def test_dry_run_and_failed_pushes_do_not_count():
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    counts = []
    for i in range(6):
        decision = engine.observe(line("recorder", "book_gap", "WARNING", kind=f"k{i}", duration_s=90))
        counts.append(len(decision.pushes))
        sent(engine, decision, NOW, Delivery.DRY_RUN if i % 2 else Delivery.FAILED)

    assert counts == [1] * 6


def test_the_cap_resets_at_the_next_clock_hour():
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=2))
    first = engine.observe(line("recorder", "book_gap", "WARNING", kind="a", duration_s=90))
    sent(engine, first, NOW)
    storm = engine.observe(line("recorder", "book_gap", "WARNING", kind="b", duration_s=90))
    sent(engine, storm, NOW)

    next_hour = NOW + timedelta(hours=1)
    fresh = engine.observe(line("recorder", "book_gap", "WARNING", at=next_hour, kind="c", duration_s=90))

    assert storm.pushes[0].episode.rule == "alert_storm"
    assert fresh.pushes[0].episode.fingerprint == "book_gap_long:c"


def test_test_pushes_ignore_the_cap():
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=2))
    sent(engine, engine.observe(line("recorder", "book_gap", "WARNING", kind="a", duration_s=90)), NOW)

    decision = engine.test_episodes("cmd", [Tier.WARNING, Tier.INFO], NOW)

    assert len(decision.pushes) == 2


def gap(i, at=NOW):
    return line("recorder", "book_gap", "WARNING", at=at, kind=f"k{i}", duration_s=90)


def sent_warning(at):
    """An open warning condition last pushed at ``at``, as a restart would load it."""
    opened = AlertEngine(CONFIG).check(CheckResult("container_unhealthy", ok=False, at=at, subject=f"c{at.minute}"))
    return replace(opened.store[0], delivery=Delivery.SENT, sends=1, last_sent_at=at)


def storms(decision):
    return [p for p in decision.pushes if p.episode.rule == "alert_storm"]


def test_renotifies_count_toward_the_cap():
    # covers: AC-10, re-notifications count
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    hour_ago = NOW - timedelta(hours=1)
    engine.load([sent_warning(hour_ago), sent_warning(hour_ago + timedelta(minutes=1))])
    renotified = engine.tick(NOW + timedelta(minutes=2))
    assert [p.reason for p in renotified.pushes] == [PushReason.RENOTIFY, PushReason.RENOTIFY]
    sent(engine, renotified, NOW + timedelta(minutes=2))

    decision = engine.observe(gap(0, NOW + timedelta(minutes=3)))

    assert len(storms(decision)) == 1


def test_critical_pushes_never_count_toward_the_cap():
    # covers: AC-10, criticals are never capped and don't count
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    for i in range(5):
        decision = engine.observe(line("trader", "order_rejected", "CRITICAL", event_key=f"stop_not_accepted:S{i}"))
        assert len(decision.pushes) == 1
        sent(engine, decision, NOW)

    warnings = [engine.observe(gap(i)) for i in range(2)]

    assert [[p.episode.rule for p in d.pushes] for d in warnings] == [["book_gap_long"], ["book_gap_long"]]


def test_muted_pushes_never_count_toward_the_cap():
    # covers: AC-10, held and muted rows don't count; AC-12, the unmute sends one summary
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))
    engine.open_hold(Hold(HoldKind.MUTE, "m1", NOW, NOW + timedelta(hours=1)))

    muted = [engine.observe(gap(i)) for i in range(10)]
    closed = engine.close_hold(HoldKind.MUTE, NOW + timedelta(minutes=1))

    assert all(d.pushes == () for d in muted)
    assert [p.reason for p in closed.pushes] == [PushReason.SUMMARY]


def test_pushes_admitted_but_not_yet_reported_hold_their_place():
    # covers: AC-10, a push still in flight counts, so a burst can't slip past the cap
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=3))

    decisions = [engine.observe(gap(i)) for i in range(3)]

    assert [p.episode.rule for d in decisions for p in d.pushes] == ["book_gap_long", "book_gap_long", "alert_storm"]


def test_a_capped_summary_leaves_what_it_covered_capped():
    # covers: AC-10, summaries count, and a capped summary's episodes are never pushed later
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=2))
    sent(engine, engine.observe(gap(0)), NOW)
    engine.open_hold(Hold(HoldKind.MUTE, "m1", NOW, NOW + timedelta(hours=1)))
    engine.observe(gap(1, NOW + timedelta(minutes=1)))

    closed = engine.close_hold(HoldKind.MUTE, NOW + timedelta(minutes=2))

    assert [p.episode.rule for p in closed.pushes] == ["alert_storm"]
    covered = [e for e in closed.store if e.fingerprint == "book_gap_long:k1"]
    assert [e.delivery for e in covered] == [Delivery.CAPPED]


def test_a_capped_renotify_is_capped_once_not_on_every_tick():
    # covers: AC-10, a capped push is never offered again, so the storm counts it once
    engine = AlertEngine(EngineConfig(env="demo", hourly_cap=2))
    engine.load([sent_warning(NOW - timedelta(hours=1))])
    sent(engine, engine.observe(gap(0)), NOW)
    sent(engine, engine.observe(gap(1)), NOW)  # the storm push

    first = engine.tick(NOW + timedelta(seconds=5))
    second = engine.tick(NOW + timedelta(seconds=10))

    assert [e.delivery for e in first.store if e.rule == "container_unhealthy"] == [Delivery.CAPPED]
    assert second.store == ()
    assert second.pushes == ()


FILL_ROW = {
    "instrument_id": "PF_XBTUSD.KRAKEN",
    "side": "buy",
    "quantity": "0.001",
    "price": "67412.5",
    "fee": "0.02",
    "fee_currency": "USD",
    "base_currency": "BTC",
    "lot_size": "0.0001",
    "tick_size": "0.5",
}


def test_a_live_fill_is_one_silent_push_with_the_fills_row():
    engine = AlertEngine(EngineConfig(env="live"))

    decision = engine.observe(line("trader", "fill_seen", "INFO", trade_id="T-1", **FILL_ROW))

    (episode,) = decision.store
    (push,) = decision.pushes
    assert (episode.fingerprint, episode.severity, push.tier) == ("fill:T-1", Tier.INFO, Tier.INFO)
    assert episode.title == "[live] Fill PF_XBTUSD.KRAKEN"
    assert episode.message == "Bought 0.0010 BTC at 67,412.5, fee 0.02 USD"


def test_a_demo_fill_pushes_and_stores_nothing():
    engine = AlertEngine(CONFIG)

    decision = engine.observe(line("trader", "fill_seen", "INFO", trade_id="T-1", **FILL_ROW))

    assert decision == Decision()


def test_a_fill_seen_twice_is_one_episode():
    engine = AlertEngine(EngineConfig(env="live"))
    sent(engine, engine.observe(line("trader", "fill_seen", "INFO", trade_id="T-1")), NOW)

    again = engine.observe(line("trader", "fill_seen", "INFO", at=NOW + timedelta(seconds=5), trade_id="T-1"))

    assert again.pushes == ()
    assert again.store[0].occurrences == 2


def test_a_fill_without_its_row_names_the_trade_id():
    engine = AlertEngine(EngineConfig(env="live"))

    (push,) = engine.observe(line("trader", "fill_seen", "INFO", trade_id="T-9")).pushes

    assert (push.title, push.message) == ("[live] Fill", "fill T-9")


def test_a_fill_from_another_service_is_ignored():
    engine = AlertEngine(EngineConfig(env="live"))

    assert engine.observe(line("recorder", "fill_seen", "INFO", trade_id="T-1")) == Decision()
