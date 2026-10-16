"""covers: spec 0013 AC-12 and AC-16 on the dashboard: the mute control appends ``alerts_mute`` and
``alerts_unmute``, the badge follows the alerter's newest mute or unmute result in Amsterdam time,
the test button waits up to 10 s for the alerter's answer, and the Activity feed lists alert
episodes between the commands (with the commands kept when Postgres fails)."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard.data.alerts import AlertLine
from dashboard.web import alert_routes
from trading_core.alerts.model import Delivery, Tier
from trading_core.control.alerts import AlertResult, command_from_fields, result_to_fields
from trading_core.control.messages import stop_command, to_fields
from trading_core.recording.enums import AlertCmd, ControlOutcome

from .conftest import log_in, make_app

STREAM = "demo:control:alerts"
RESULTS = "demo:control:alerts:results"
HTMX = {"HX-Request": "true"}


def _csrf(client):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _commands(redis):
    return [command_from_fields(fields) for _, fields in redis.streams.get(STREAM, [])]


def _now(clock):
    return datetime.fromtimestamp(clock.now, tz=UTC)


def _feed(page):
    match = re.search(r'<div class="activity-feed" sse-swap="overview-activity">(.*?)</div>\s*</section>', page, re.S)
    assert match, "no Activity feed on Overview"
    return match.group(1)


def _badge(page):
    match = re.search(r'<span class="activity-mute" sse-swap="overview-mute">(.*?)</span></header>', page, re.S)
    assert match, "no mute badge slot on Overview"
    return match.group(1)


# --- the mute control


@pytest.mark.parametrize(("hours", "mute_s"), [("1", 3600), ("4", 14_400), ("12", 43_200)])
def test_mute_appends_one_alerts_mute_with_its_length(logged_in, redis, hours, mute_s):
    response = logged_in.post("/alerts/mute", data={"csrf": _csrf(logged_in), "hours": hours}, headers=HTMX)

    assert response.status_code == 200
    assert f"Mute for {hours} h sent" in response.text
    assert 'id="alerts-area"' in response.text
    (msg,) = _commands(redis)
    assert (msg.cmd, msg.mute_s) == (AlertCmd.MUTE, mute_s)
    assert redis.xadd_calls[-1]["maxlen"] == 1000


def test_unmute_appends_alerts_unmute(logged_in, redis):
    response = logged_in.post("/alerts/mute", data={"csrf": _csrf(logged_in), "hours": "0"}, headers=HTMX)

    assert "Unmute sent" in response.text
    (msg,) = _commands(redis)
    assert (msg.cmd, msg.mute_s) == (AlertCmd.UNMUTE, None)


@pytest.mark.parametrize("hours", ["2", "24", "-1", "x"])
def test_any_other_length_is_422_and_sends_nothing(logged_in, redis, hours):
    response = logged_in.post("/alerts/mute", data={"csrf": _csrf(logged_in), "hours": hours}, headers=HTMX)

    assert response.status_code == 422
    assert _commands(redis) == []


def test_a_mute_without_the_csrf_token_is_not_sent(logged_in, redis):
    response = logged_in.post("/alerts/mute", data={"csrf": "wrong", "hours": "1"}, headers=HTMX)

    assert response.status_code == 403
    assert "NOT sent: reload the page" in response.text
    assert 'id="alerts-area"' in response.text
    assert _commands(redis) == []


def test_a_mute_redis_refuses_says_not_sent(logged_in, redis):
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")

    response = logged_in.post("/alerts/mute", data={"csrf": csrf, "hours": "1"}, headers=HTMX)

    assert response.status_code == 503
    assert "Mute for 1 h NOT sent: Redis did not answer" in response.text


def test_a_plain_form_mute_goes_back_to_the_page(logged_in, redis):
    response = logged_in.post(
        "/alerts/mute", data={"csrf": _csrf(logged_in), "hours": "1"}, headers={"Referer": "http://testserver/"}
    )

    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert len(_commands(redis)) == 1


def test_a_mute_without_a_session_goes_to_login(client, redis):
    response = client.post("/alerts/mute", data={"hours": "1"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert _commands(redis) == []


# --- the badge


def _result(clock, cmd, *, until=None, outcome=ControlOutcome.EXECUTED, ago_s=0):
    from uuid import uuid4

    at = _now(clock) - timedelta(seconds=ago_s)
    return result_to_fields(AlertResult(uuid4(), outcome, at, cmd, until=until))


def test_the_badge_shows_the_newest_mute_in_amsterdam_time(logged_in, redis, clock):
    until = (_now(clock) + timedelta(hours=1)).replace(microsecond=0)
    redis.append(RESULTS, _result(clock, AlertCmd.MUTE, until=until))

    badge = _badge(logged_in.get("/").text)

    local = until.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M")
    utc = until.strftime("%H:%M:%SZ")
    assert "Warnings muted until" in badge
    assert f'<time datetime="{until.strftime("%Y-%m-%dT%H:%M:%SZ")}" title="{utc} UTC">{local}</time>' in badge


@pytest.mark.parametrize(
    "newest",
    [
        {"cmd": AlertCmd.UNMUTE},
        {"cmd": AlertCmd.MUTE, "until": "past"},
    ],
)
def test_the_badge_hides_after_an_unmute_or_once_the_mute_is_over(logged_in, redis, clock, newest):
    redis.append(RESULTS, _result(clock, AlertCmd.MUTE, until=_now(clock) + timedelta(hours=4), ago_s=60))
    until = _now(clock) - timedelta(seconds=1) if newest.get("until") == "past" else None
    redis.append(RESULTS, _result(clock, newest["cmd"], until=until))

    assert "muted" not in _badge(logged_in.get("/").text)


def test_a_failed_mute_does_not_change_the_badge(logged_in, redis, clock):
    redis.append(RESULTS, _result(clock, AlertCmd.MUTE, until=_now(clock) + timedelta(hours=1), ago_s=60))
    redis.append(RESULTS, _result(clock, AlertCmd.UNMUTE, outcome=ControlOutcome.FAILED))

    assert "Warnings muted until" in _badge(logged_in.get("/").text)


# --- the test button


def _answering(redis, monkeypatch, outcome, reason=None):
    """The alerter answers each test command on its results stream as soon as it is appended."""
    append = redis.xadd

    async def xadd(name, fields, **kwargs):
        entry = await append(name, fields, **kwargs)
        if name == STREAM:
            msg = command_from_fields(fields)
            redis.append(RESULTS, result_to_fields(AlertResult(msg.id, outcome, msg.issued_at, msg.cmd, reason)))
        return entry

    monkeypatch.setattr(redis, "xadd", xadd)


def test_the_test_button_says_test_sent_once_the_alerter_answers(logged_in, redis, monkeypatch):
    csrf = _csrf(logged_in)
    _answering(redis, monkeypatch, ControlOutcome.EXECUTED)

    response = logged_in.post("/alerts/test", data={"csrf": csrf}, headers=HTMX)

    assert response.status_code == 200
    assert "Test sent: one push per tier" in response.text
    (msg,) = _commands(redis)
    assert msg.cmd is AlertCmd.TEST


def test_the_test_button_names_the_failure(logged_in, redis, monkeypatch):
    csrf = _csrf(logged_in)
    _answering(redis, monkeypatch, ControlOutcome.FAILED, "push_failed")

    response = logged_in.post("/alerts/test", data={"csrf": csrf}, headers=HTMX)

    assert "Test failed: a push failed, see the Activity feed" in response.text


def test_the_test_button_gives_up_when_the_alerter_never_answers(logged_in, redis, monkeypatch):
    monkeypatch.setattr(alert_routes, "TEST_WAIT_S", 0.2)
    monkeypatch.setattr(alert_routes, "TEST_POLL_S", 0.05)

    response = logged_in.post("/alerts/test", data={"csrf": _csrf(logged_in)}, headers=HTMX)

    assert response.status_code == 200
    assert "no answer from the alerter" in response.text


def test_the_test_button_with_redis_down_says_not_sent(logged_in, redis):
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")

    response = logged_in.post("/alerts/test", data={"csrf": csrf}, headers=HTMX)

    assert response.status_code == 503
    assert "Test NOT sent: Redis did not answer" in response.text


def test_the_test_waits_ten_seconds_for_an_answer():
    assert alert_routes.TEST_WAIT_S == 10.0


# --- the Activity feed


def _alert(clock, minutes_ago, severity="critical", title="Stop not accepted", resolved=False, delivery="sent"):
    opened = _now(clock).replace(microsecond=0) - timedelta(minutes=minutes_ago)
    return AlertLine(minutes_ago, opened, Tier(severity), title, resolved, Delivery(delivery))


def _overview(redis, clock, read_alerts):
    with TestClient(make_app(redis, clock, read_alerts=read_alerts), follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        return client.get("/").text


def test_alert_episodes_show_with_tier_title_state_and_delivery(redis, clock):
    alerts = [
        _alert(clock, 1),
        _alert(clock, 5, "warning", "Context feed down on recorder", delivery="muted"),
        _alert(clock, 9, "info", "Resolved: Trader down", resolved=True, delivery="dry_run"),
    ]

    feed = _feed(_overview(redis, clock, lambda: alerts))

    assert feed.index("Stop not accepted") < feed.index("Context feed down") < feed.index("Resolved: Trader down")
    assert '<span class="badge badge--bad">critical</span>' in feed
    assert '<span class="badge badge--warn">warning</span>' in feed
    assert "open · sent" in feed
    assert "open · muted" in feed
    assert "resolved · dry run" in feed


def test_alerts_and_commands_share_one_list_by_time(redis, clock):
    redis.append("demo:control", to_fields(stop_command(_now(clock) - timedelta(minutes=3), 300)))

    feed = _feed(_overview(redis, clock, lambda: [_alert(clock, 1), _alert(clock, 10, title="Disk space low")]))

    assert feed.index("Stop not accepted") < feed.index(">stop<") < feed.index("Disk space low")


def test_a_failed_push_is_red(redis, clock):
    feed = _feed(_overview(redis, clock, lambda: [_alert(clock, 1, delivery="failed")]))

    assert 'command--bad">open · push failed' in feed


def test_postgres_down_keeps_the_commands_and_says_alerts_are_unavailable(redis, clock):
    redis.append("demo:control", to_fields(stop_command(_now(clock), 300)))

    def down():
        raise ConnectionError("postgres down")

    feed = _feed(_overview(redis, clock, down))

    assert "unavailable: Postgres did not answer" in feed
    assert ">stop<" in feed


def test_the_overview_has_the_mute_and_test_controls(logged_in):
    page = logged_in.get("/").text

    for label in ("1 h", "4 h", "12 h", "Unmute", "Send test alert"):
        assert label in page
    assert 'action="/alerts/mute"' in page
    assert 'action="/alerts/test"' in page
    assert "critical alerts always reach your phone" in page
