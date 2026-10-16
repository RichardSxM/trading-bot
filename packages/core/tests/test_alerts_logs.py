"""covers: spec 0013 AC-3 and AC-7 (log lines become episodes), AC-4 (service, container, and
storage conditions), AC-9 (repeats bump, pairs resolve), and AC-10 (the hourly storm cap).

Pure: no clock, no I/O.
"""

from datetime import UTC, datetime, timedelta

from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import CheckResult, Decision, Delivery, Episode, Observation, Push, PushReason, Tier

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
