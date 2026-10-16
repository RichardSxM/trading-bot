"""covers: spec 0013 AC-1 (trader down and its note), AC-9 (one push per episode, re-notify, restart
load), AC-14 (failed pushes and their retry), AC-17 (test episodes), and the texts' cut and redaction.

Pure: no clock, no I/O.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trading_core.alerts import messages
from trading_core.alerts.checks import heartbeat_check
from trading_core.alerts.engine import AlertEngine, AlertEngineError, EngineConfig
from trading_core.alerts.model import CheckResult, Delivery, PushReason, Tier
from trading_core.secrets import read_secret

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
CONFIG = EngineConfig(env="demo")


def _ns(at):
    return int(at.timestamp()) * 1_000_000_000


def down(at, age_s=50):
    return CheckResult("trader_down", ok=False, at=at, fields={"age_s": age_s})


def up(at):
    return CheckResult("trader_down", ok=True, at=at, fields={"age_s": 1})


def send_all(engine, decision, at, delivery=Delivery.SENT):
    """Report every push in ``decision`` as delivered; returns the stored episodes."""
    return [engine.record(push, delivery, at) for push in decision.pushes]


# AC-1 -----------------------------------------------------------------------------------------


def test_a_stale_heartbeat_opens_one_critical_push():
    engine = AlertEngine(CONFIG)

    decision = engine.check(down(NOW))

    assert len(decision.store) == 1
    episode = decision.store[0]
    assert (episode.rule, episode.fingerprint, episode.severity) == ("trader_down", "trader_down", Tier.CRITICAL)
    assert episode.title == "[demo] Trader down"
    assert episode.delivery is Delivery.PENDING
    [push] = decision.pushes
    assert (push.reason, push.tier) == (PushReason.OPEN, Tier.CRITICAL)


def test_a_fresh_heartbeat_resolves_it_with_a_silent_note():
    engine = AlertEngine(CONFIG)
    send_all(engine, engine.check(down(NOW)), NOW)

    decision = engine.check(up(NOW + timedelta(minutes=4)))

    [stored] = decision.store
    assert stored.resolved_at == NOW + timedelta(minutes=4)
    [note] = decision.pushes
    assert (note.reason, note.tier) == (PushReason.RESOLVED, Tier.INFO)
    assert note.title == "[demo] Resolved: Trader down"
    assert note.message == "Resolved after 4 min."
    assert engine.open_episodes == ()


def test_an_episode_never_pushed_resolves_without_a_note():
    engine = AlertEngine(CONFIG)
    engine.check(down(NOW))  # the push is still in flight, nothing recorded

    decision = engine.check(up(NOW + timedelta(seconds=10)))

    assert decision.pushes == ()
    assert decision.store[0].resolved_at is not None


def test_a_passing_check_with_nothing_open_does_nothing():
    assert AlertEngine(CONFIG).check(up(NOW)).store == ()


@pytest.mark.parametrize(
    ("ts_ns", "ok", "fields"),
    [
        (None, False, {"missing": True}),
        (_ns(NOW - timedelta(seconds=46)), False, {"age_s": 46}),
        (_ns(NOW - timedelta(seconds=45)), True, {"age_s": 45}),
        (_ns(NOW + timedelta(seconds=30)), True, {"age_s": 0}),  # clock skew counts as fresh
    ],
)
def test_heartbeat_check(ts_ns, ok, fields):
    result = heartbeat_check(ts_ns, NOW, threshold_s=45)

    assert (result.rule, result.ok, dict(result.fields)) == ("trader_down", ok, fields)


def test_a_missing_heartbeat_says_so_in_plain_words():
    decision = AlertEngine(CONFIG).check(heartbeat_check(None, NOW, 45))

    assert decision.store[0].message == "No trader heartbeat in Redis."


# AC-9 -----------------------------------------------------------------------------------------


def test_a_condition_still_failing_pushes_nothing_more():
    engine = AlertEngine(CONFIG)
    send_all(engine, engine.check(down(NOW)), NOW)

    for seconds in range(5, 600, 5):
        at = NOW + timedelta(seconds=seconds)
        assert engine.check(down(at)).pushes == ()
        assert engine.tick(at).pushes == ()
    assert len(engine.open_episodes) == 1


def test_a_push_in_flight_is_not_sent_twice_by_a_tick():
    engine = AlertEngine(CONFIG)
    engine.check(down(NOW))

    assert engine.tick(NOW + timedelta(seconds=5)).pushes == ()


def test_a_critical_condition_repushes_after_four_hours():
    engine = AlertEngine(CONFIG)
    send_all(engine, engine.check(down(NOW)), NOW)

    assert engine.tick(NOW + timedelta(hours=4) - timedelta(seconds=1)).pushes == ()
    [push] = engine.tick(NOW + timedelta(hours=4)).pushes
    assert push.reason is PushReason.RENOTIFY
    assert push.tier is Tier.CRITICAL
    assert engine.tick(NOW + timedelta(hours=4)).pushes == ()  # still in flight
    stored = engine.record(push, Delivery.SENT, NOW + timedelta(hours=4))
    assert stored is not None
    assert (stored.sends, stored.last_sent_at) == (2, NOW + timedelta(hours=4))


def test_a_warning_renotifies_hourly_with_its_count():
    engine = AlertEngine(CONFIG)
    episode = replace(
        engine.check(down(NOW)).store[0], severity=Tier.WARNING, sends=1, last_sent_at=NOW, delivery=Delivery.SENT
    )
    engine = AlertEngine(CONFIG)
    engine.load([replace(episode, occurrences=7)])

    [push] = engine.tick(NOW + timedelta(hours=1)).pushes

    assert push.reason is PushReason.RENOTIFY
    assert "seen 7 times since 03:00 UTC" in push.message


def test_a_restart_loads_open_episodes_and_pushes_nothing_twice():
    first = AlertEngine(CONFIG)
    [stored] = send_all(first, first.check(down(NOW)), NOW)

    second = AlertEngine(CONFIG)
    second.load([stored])

    later = NOW + timedelta(seconds=30)
    assert second.check(down(later)).store == ()
    assert second.tick(later).pushes == ()
    assert len(second.open_episodes) == 1
    # And it still resolves with its note, because it was pushed before the restart.
    assert second.check(up(later)).pushes[0].reason is PushReason.RESOLVED


def test_load_skips_the_other_env_and_keeps_what_is_already_open():
    engine = AlertEngine(CONFIG)
    [live] = AlertEngine(EngineConfig(env="live")).check(down(NOW)).store
    engine.load([live])
    assert engine.open_episodes == ()

    current = engine.check(down(NOW)).store[0]
    engine.load([replace(current, opened_at=NOW - timedelta(hours=1))])
    assert list(engine.open_episodes) == [current]


def test_a_dry_run_counts_as_a_send():
    engine = AlertEngine(CONFIG)

    [stored] = send_all(engine, engine.check(down(NOW)), NOW, Delivery.DRY_RUN)

    assert (stored.delivery, stored.sends) == (Delivery.DRY_RUN, 1)


# AC-14 ----------------------------------------------------------------------------------------


def test_a_failed_push_is_retried_and_flags_the_watchdog():
    engine = AlertEngine(CONFIG)
    [failed] = send_all(engine, engine.check(down(NOW)), NOW, Delivery.FAILED)

    assert (failed.delivery, failed.sends) == (Delivery.FAILED, 0)
    assert engine.push_failing
    assert engine.tick(NOW + timedelta(hours=5)).pushes == ()  # the retry cycle owns it, not re-notify

    retry = engine.retry(NOW + timedelta(minutes=1))
    assert [p.episode.fingerprint for p in retry.pushes] == ["trader_down"]
    assert engine.retry(NOW + timedelta(minutes=1)).pushes == ()  # in flight

    [sent] = send_all(engine, retry, NOW + timedelta(minutes=1))
    assert (sent.delivery, sent.sends) == (Delivery.SENT, 1)
    assert not engine.push_failing


def test_retries_go_critical_first_then_oldest_and_stop_after_a_day():
    engine = AlertEngine(EngineConfig(env="demo", retry_limit=2))
    base = engine.check(down(NOW)).store[0]
    old_warning = replace(base, fingerprint="w1", severity=Tier.WARNING, delivery=Delivery.FAILED)
    new_critical = replace(base, fingerprint="c1", opened_at=NOW + timedelta(minutes=5), delivery=Delivery.FAILED)
    stale = replace(base, fingerprint="c0", opened_at=NOW - timedelta(days=2), delivery=Delivery.FAILED)
    engine.load([old_warning, new_critical, stale])

    retry = engine.retry(NOW + timedelta(minutes=10))

    assert [p.episode.fingerprint for p in retry.pushes] == ["c1", "w1"]


# AC-17 ----------------------------------------------------------------------------------------


def test_test_episodes_are_one_marked_push_per_tier_and_stored_resolved():
    engine = AlertEngine(CONFIG)

    decision = engine.test_episodes("abc", list(Tier), NOW)

    assert [p.tier for p in decision.pushes] == [Tier.INFO, Tier.WARNING, Tier.CRITICAL]
    assert {e.fingerprint for e in decision.store} == {"test:abc:info", "test:abc:warning", "test:abc:critical"}
    assert all(e.resolved_at == NOW and "TEST" in e.title for e in decision.store)
    stored = send_all(engine, decision, NOW, Delivery.DRY_RUN)
    assert all(e.delivery is Delivery.DRY_RUN and e.sends == 1 for e in stored)
    assert engine.open_episodes == ()


# Guards and texts -----------------------------------------------------------------------------


def test_the_engine_refuses_naive_times_and_unknown_rules():
    engine = AlertEngine(CONFIG)
    with pytest.raises(AlertEngineError):
        engine.check(down(NOW.replace(tzinfo=None)))
    with pytest.raises(AlertEngineError):
        engine.check(CheckResult("nope", ok=False, at=NOW))
    with pytest.raises(AlertEngineError):
        engine.check(CheckResult("test", ok=False, at=NOW))


def test_texts_are_cut_with_an_ellipsis():
    assert len(messages.title("demo", "x" * 400)) == messages.TITLE_MAX
    assert messages.title("demo", "x" * 400).endswith("…")
    assert len(messages.message("y" * 2000)) == messages.MESSAGE_MAX
    assert messages.message("short") == "short"


def test_texts_and_details_are_redacted(tmp_path):
    (tmp_path / "pushover_app_token").write_text("azGDORePK8gMaC0QOYAMyEEuzJnyUi")
    read_secret("pushover_app_token", tmp_path)

    assert "azGDOReP" not in messages.message("token azGDORePK8gMaC0QOYAMyEEuzJnyUi leaked")
    details = messages.redact_details(
        {"url": "x azGDORePK8gMaC0QOYAMyEEuzJnyUi", "n": [1, "azGDORePK8gMaC0QOYAMyEEuzJnyUi"]}
    )
    assert "azGDOReP" not in str(details)


@pytest.mark.parametrize(("seconds", "words"), [(0, "0 s"), (59, "59 s"), (60, "1 min"), (150, "2 min")])
def test_duration_words(seconds, words):
    assert messages.duration(NOW, NOW + timedelta(seconds=seconds)) == words
