"""covers: spec 0013 AC-4 (host and deploy rows), AC-11 (the deploy window), and the summary push.

Pure: no clock, no I/O.
"""

from datetime import UTC, datetime, timedelta

from trading_core.alerts import messages
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import CheckResult, Delivery, HoldKind, Observation, PushReason, Tier
from trading_core.alerts.rules import first_match

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
CONFIG = EngineConfig(env="demo")
SHA = "4d55abd0123456789"


def host(msg, at=NOW, **fields):
    return Observation("host", "host", at, None, fields.pop("level", "INFO"), {"msg": msg, **fields})


def started(at=NOW, sha=SHA, started_at=None):
    return host("deploy started", at, sha=sha, started_at=started_at or at.strftime("%Y-%m-%dT%H:%M:%SZ"))


def outcome(at, sha=SHA, started_at=NOW, result="deployed"):
    fields = {"sha": sha, "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "outcome": result}
    return Observation("deploy", "deploy", at, None, None, fields)


def warning(at):
    return Observation(
        "log:trader", "trader", at, "kraken_key_rotation_due", "WARNING", {"code": "kraken_key_rotation_due"}
    )


def critical(at):
    return Observation("log:trader", "trader", at, "kraken_auth_failed", "CRITICAL", {"code": "kraken_auth_failed"})


def unhealthy(at, ok=False):
    return CheckResult("container_unhealthy", ok=ok, at=at, subject="recorder", fields={"status": "Up (unhealthy)"})


def trader_down(at, ok=False):
    return CheckResult("trader_down", ok=ok, at=at, fields={"age_s": 1 if ok else 50})


def send_all(engine, decision, at, delivery=Delivery.SENT):
    return [stored for push in decision.pushes for stored in engine.record(push, delivery, at)]


# Host and deploy rows (AC-4) -----------------------------------------------------------------


def test_autoheal_giving_up_is_critical_per_container():
    obs = host("autoheal_gave_up", level="CRITICAL", container="kfs-recorder-1", restarts_last_hour="3")

    rule = first_match(obs)

    assert rule is not None
    assert (rule.id, rule.tier) == ("autoheal_gave_up", Tier.CRITICAL)
    assert rule.key is not None
    assert rule.key(obs) == "autoheal_gave_up:kfs-recorder-1"


def test_a_failed_rollback_is_one_critical_push_from_its_outcome_line():
    obs = outcome(NOW, result="rollback_failed")

    rule = first_match(obs)

    assert rule is not None
    assert rule.id == "rollback_failed"
    assert rule.key is not None
    assert rule.key(obs) == f"rollback_failed:{SHA}"
    # The host's own "rollback failed" line is not a second push.
    assert first_match(host("rollback failed", level="CRITICAL", sha=SHA)) is None


def test_a_container_line_with_the_same_message_is_not_a_host_line():
    obs = Observation("log:trader", "trader", NOW, None, "CRITICAL", {"msg": "autoheal_gave_up"})

    assert first_match(obs) is None


# The deploy window (AC-11) -------------------------------------------------------------------


def test_a_warning_during_a_deploy_is_held_then_sent_in_one_summary():
    engine = AlertEngine(CONFIG)
    assert engine.observe(started()).pushes == ()
    [hold] = engine.holds
    assert hold.kind is HoldKind.DEPLOY

    decision = engine.observe(warning(NOW + timedelta(minutes=1)))

    assert decision.pushes == ()
    assert decision.store[-1].delivery is Delivery.HELD

    end = engine.observe(outcome(NOW + timedelta(minutes=4)))
    [push] = end.pushes
    assert push.reason is PushReason.SUMMARY
    assert push.tier is Tier.WARNING
    assert push.title == "[demo] 1 warning during deploy"
    assert push.message == "Kraken key rotation due on trader"
    assert engine.holds == ()

    stored = send_all(engine, end, NOW + timedelta(minutes=4))
    summary, covered = stored
    assert summary.rule == "summary"
    assert summary.fingerprint.startswith("summary:deploy:4d55abd01234:")
    assert summary.details["covers"] == ["kraken_key_rotation_due"]
    assert (covered.delivery, covered.sends) == (Delivery.SENT, 1)


def test_a_held_warning_that_cleared_is_dropped():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    engine.check(unhealthy(NOW))
    held = engine.tick(NOW + timedelta(seconds=130))
    assert held.pushes == ()
    assert held.store[-1].delivery is Delivery.HELD
    engine.check(unhealthy(NOW + timedelta(seconds=200), ok=True))

    end = engine.observe(outcome(NOW + timedelta(minutes=5)))

    assert end.pushes == ()


def test_held_info_is_dropped_at_the_end():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    halt = Observation("log:trader", "trader", NOW, "halt", "WARNING", {"code": "halt", "event_key": "halt:H-1"})
    assert engine.observe(halt).pushes == ()

    assert engine.observe(outcome(NOW + timedelta(minutes=3))).pushes == ()


def test_a_critical_goes_straight_through_a_deploy():
    engine = AlertEngine(CONFIG)
    engine.observe(started())

    [push] = engine.observe(critical(NOW + timedelta(seconds=30))).pushes

    assert push.tier is Tier.CRITICAL


def test_trader_down_is_held_and_a_normal_restart_resolves_it_silently():
    engine = AlertEngine(CONFIG)
    engine.observe(started())

    opened = engine.check(trader_down(NOW + timedelta(seconds=60)))
    assert opened.pushes == ()
    assert opened.store[-1].delivery is Delivery.HELD
    resolved = engine.check(trader_down(NOW + timedelta(seconds=120), ok=True))
    assert resolved.pushes == ()
    assert resolved.store[-1].resolved_at is not None

    assert engine.observe(outcome(NOW + timedelta(minutes=4))).pushes == ()


def test_trader_down_still_open_when_the_window_ends_is_pushed_then():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    engine.check(trader_down(NOW + timedelta(seconds=60)))

    end = engine.observe(outcome(NOW + timedelta(minutes=4)))

    [push] = end.pushes
    assert (push.episode.rule, push.tier, push.reason) == ("trader_down", Tier.CRITICAL, PushReason.OPEN)


def test_the_window_ends_after_fifteen_minutes_without_its_outcome_line():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    engine.observe(warning(NOW + timedelta(minutes=1)))

    assert engine.tick(NOW + timedelta(minutes=15) - timedelta(seconds=1)).pushes == ()
    [push] = engine.tick(NOW + timedelta(minutes=15)).pushes

    assert push.reason is PushReason.SUMMARY
    assert engine.holds == ()


def test_only_the_same_sha_started_in_the_window_closes_it():
    engine = AlertEngine(CONFIG)
    engine.observe(started())

    engine.observe(outcome(NOW + timedelta(minutes=1), sha="other"))
    engine.observe(outcome(NOW + timedelta(minutes=1), started_at=NOW - timedelta(hours=1)))

    assert len(engine.holds) == 1
    engine.observe(outcome(NOW + timedelta(minutes=2)))
    assert len(engine.holds) == 0


def test_a_second_deploy_start_replaces_the_window_and_keeps_what_it_held():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    engine.observe(warning(NOW + timedelta(minutes=1)))
    later = NOW + timedelta(minutes=2)
    engine.observe(started(later, sha="feedface00000000"))

    assert engine.observe(outcome(later + timedelta(minutes=1))).pushes == ()  # the old sha
    [push] = engine.observe(outcome(later + timedelta(minutes=1), sha="feedface00000000", started_at=later)).pushes
    assert push.title == "[demo] 1 warning during deploy"


def test_the_outcome_line_still_alerts_when_it_closes_the_window():
    engine = AlertEngine(CONFIG)
    engine.observe(started())

    decision = engine.observe(outcome(NOW + timedelta(minutes=3), result="rollback_failed"))

    [push] = decision.pushes
    assert push.episode.rule == "rollback_failed"
    assert engine.holds == ()


def test_a_failed_summary_retries_and_then_marks_what_it_covers():
    engine = AlertEngine(CONFIG)
    engine.observe(started())
    engine.observe(warning(NOW + timedelta(minutes=1)))
    end = engine.observe(outcome(NOW + timedelta(minutes=3)))
    [failed] = send_all(engine, end, NOW + timedelta(minutes=3), Delivery.FAILED)
    assert failed.rule == "summary"

    retry = engine.retry(NOW + timedelta(minutes=4))
    stored = send_all(engine, retry, NOW + timedelta(minutes=4))

    assert [(e.rule, e.delivery) for e in stored] == [
        ("summary", Delivery.SENT),
        ("kraken_key_rotation_due", Delivery.SENT),
    ]


def test_a_held_warning_does_not_renotify_while_held():
    engine = AlertEngine(CONFIG)
    engine.check(unhealthy(NOW))
    sent_at = NOW + timedelta(seconds=120)
    send_all(engine, engine.tick(sent_at), sent_at)
    engine.observe(started(sent_at + timedelta(minutes=50)))
    # The hourly re-notify falls inside the window: held once, then quiet.
    held = engine.tick(sent_at + timedelta(hours=1))
    assert held.pushes == ()
    assert held.store[-1].delivery is Delivery.HELD
    assert engine.tick(sent_at + timedelta(hours=1, seconds=5)).store == ()


def test_summary_words():
    titles = [f"[live] Thing {i}" for i in range(12)]

    head, body = messages.summary(HoldKind.MUTE, titles)

    assert head == "12 warnings while muted"
    assert body.splitlines()[0] == "Thing 0"
    assert body.splitlines()[-1] == "and 2 more"
    assert len(body.splitlines()) == 11
