"""covers: spec 0004 AC-11, AC-12, AC-13, the stop button and the command state in the top bar."""

import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard.auth.next_path import PAGE_PATHS
from dashboard.web.page_routes import PAGES
from trading_core.control.messages import format_utc
from trading_core.snapshot.envelope import encode

STREAM = "demo:control"
RESULTS = "demo:control:results"
HTMX = {"HX-Request": "true"}
REFERER = {"Referer": "http://testserver/"}


def _csrf(client):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _entries(redis):
    return [{k.decode(): v.decode() for k, v in fields.items()} for _, fields in redis.streams.get(STREAM, [])]


def test_one_tap_appends_exactly_one_stop_with_every_field(logged_in, redis, clock):
    response = logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=HTMX)

    assert response.status_code == 200
    [entry] = _entries(redis)
    assert set(entry) == {"id", "cmd", "issued_at", "expires_at", "schema_version"}
    assert uuid.UUID(entry["id"]).version == 4
    assert entry["cmd"] == "stop"
    assert entry["schema_version"] == "1"
    issued = datetime.fromisoformat(entry["issued_at"])
    assert entry["issued_at"].endswith("Z")
    assert entry["expires_at"].endswith("Z")
    assert abs(issued.timestamp() - clock.now) < 1
    assert datetime.fromisoformat(entry["expires_at"]) - issued == timedelta(seconds=300)


def test_the_htmx_reply_shows_awaiting_trader_at_once(logged_in):
    response = logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=HTMX)

    assert 'id="stop-area"' in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "stop sent · awaiting trader" in response.text
    assert "NOT sent" not in response.text


def test_every_tap_appends_there_is_no_debounce(logged_in, redis):
    csrf = _csrf(logged_in)
    for _ in range(3):
        logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)

    entries = _entries(redis)
    assert len(entries) == 3
    assert len({e["id"] for e in entries}) == 3


def test_the_csrf_header_works_too(logged_in, redis):
    response = logged_in.post("/control/stop", headers={**HTMX, "X-CSRF-Token": _csrf(logged_in)})
    assert response.status_code == 200
    assert len(_entries(redis)) == 1


def test_without_javascript_the_form_post_returns_to_the_page(logged_in, redis):
    response = logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=REFERER)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(_entries(redis)) == 1
    page = logged_in.get("/")
    assert "stop sent · awaiting trader" in page.text


def test_a_plain_form_post_never_redirects_off_site(logged_in):
    response = logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers={"Referer": "http://evil//x"})
    assert response.headers["location"] == "/"


def test_a_retry_from_the_not_sent_page_returns_to_a_real_page_not_a_405(logged_in, redis):
    csrf = _csrf(logged_in)
    retry = {"Referer": "http://testserver/control/stop"}
    redis.fail = RedisConnectionError("down")
    failed = logged_in.post("/control/stop", data={"csrf": csrf}, headers=retry)
    assert failed.status_code == 503
    assert "<title>DEMO · Overview</title>" in failed.text

    redis.fail = None
    sent = logged_in.post("/control/stop", data={"csrf": csrf}, headers=retry)

    assert sent.status_code == 303
    assert sent.headers["location"] == "/"
    assert logged_in.get(sent.headers["location"]).status_code == 200
    assert len(_entries(redis)) == 1


def test_a_missing_or_wrong_csrf_token_is_refused_and_appends_nothing(logged_in, redis):
    missing = logged_in.post("/control/stop", headers=HTMX)
    wrong = logged_in.post("/control/stop", data={"csrf": "wrong"}, headers=HTMX)
    plain = logged_in.post("/control/stop", data={"csrf": "wrong"}, headers=REFERER)

    for response in (missing, wrong, plain):
        assert response.status_code == 403
        assert "Stop NOT sent: reload the page" in response.text
        assert "awaiting trader" not in response.text
    assert 'class="topbar"' in plain.text  # the full page without JavaScript
    assert _entries(redis) == []


def test_redis_down_answers_503_logs_error_and_never_says_sent(logged_in, redis, caplog):
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")

    with caplog.at_level(logging.ERROR):
        htmx = logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)
        plain = logged_in.post("/control/stop", data={"csrf": csrf}, headers=REFERER)

    for response in (htmx, plain):
        assert response.status_code == 503
        assert "Stop NOT sent" in response.text
        assert "Retry stop" in response.text
        assert "sent ·" not in response.text
    assert 'class="topbar"' in plain.text
    codes = [r.fields["code"] for r in caplog.records if r.levelno == logging.ERROR]
    assert codes == ["control_send_failed", "control_send_failed"]
    assert all(set(r.fields) >= {"id", "cmd"} for r in caplog.records if r.levelno == logging.ERROR)


def test_a_redis_that_does_not_answer_within_a_second_is_down(logged_in, redis):
    csrf = _csrf(logged_in)
    redis.hang = 1.5

    response = logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)

    assert response.status_code == 503
    assert "Stop NOT sent" in response.text


def test_without_javascript_the_not_sent_page_does_not_read_redis_again(logged_in, redis):
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")
    redis.reads = 0

    response = logged_in.post("/control/stop", data={"csrf": csrf}, headers=REFERER)

    assert response.status_code == 503
    assert "Stop NOT sent" in response.text
    assert 'class="topbar"' in response.text
    assert redis.reads == 0


def test_a_stop_without_a_session_goes_to_login_saying_it_was_not_sent(client, redis):
    htmx = client.post("/control/stop", headers={**HTMX, **REFERER})
    plain = client.post("/control/stop", headers=REFERER)

    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"] == "/login?next=%2F&stop_lost=1"
    assert plain.status_code == 303
    assert plain.headers["location"] == "/login?next=%2F&stop_lost=1"
    assert "Your stop was NOT sent" in client.get(plain.headers["location"]).text
    assert _entries(redis) == []


def test_a_lost_stop_retried_from_the_not_sent_page_logs_in_to_a_real_page(client, redis):
    response = client.post("/control/stop", headers={"Referer": "http://testserver/control/stop"})

    assert response.headers["location"] == "/login?next=%2F&stop_lost=1"
    assert _entries(redis) == []


def test_every_shell_page_is_a_referer_page():
    assert set(PAGES) == PAGE_PATHS


def test_the_command_state_survives_a_reload_and_shows_the_result(logged_in, redis, clock):
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=HTMX)
    [entry] = _entries(redis)

    assert "stop sent · awaiting trader" in logged_in.get("/").text
    redis.append(
        RESULTS,
        {
            "id": entry["id"],
            "outcome": "executed",
            "outcome_at": format_utc(datetime.fromtimestamp(clock.now, tz=UTC)),
            "schema_version": "1",
        },
    )
    assert "stop sent · confirmed" in logged_in.get("/").text
    clock.now += 901
    page = logged_in.get("/")
    assert "stop sent" not in page.text
    assert "command--none" in page.text


def test_the_shell_has_the_stop_button_and_the_network_error_template(logged_in):
    page = logged_in.get("/").text
    assert 'action="/control/stop"' in page
    assert 'hx-post="/control/stop"' in page
    assert 'id="topbar-command" sse-swap="topbar-command"' in page
    assert '<template id="stop-failed">' in page
    assert "hx-disabled-elt" not in page


# --- gaps found against spec 0004 (/test pass)


def test_the_append_trims_the_stream_to_about_1000(logged_in, redis):
    """covers: AC-11 (XADD MAXLEN ~ 1000)"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=HTMX)
    assert redis.xadd_calls == [{"name": STREAM, "maxlen": 1000, "approximate": True}]


def test_the_command_ttl_comes_from_settings(redis, clock):
    """covers: AC-11 (expires_at = issued_at + CONTROL_COMMAND_TTL_S)"""
    from fastapi.testclient import TestClient

    from .conftest import log_in, make_app

    with TestClient(make_app(redis, clock, control_command_ttl_s=60), follow_redirects=False) as client:
        log_in(client, clock)
        client.post("/control/stop", data={"csrf": _csrf(client)}, headers=HTMX)
    [entry] = _entries(redis)
    issued = datetime.fromisoformat(entry["issued_at"])
    assert datetime.fromisoformat(entry["expires_at"]) - issued == timedelta(seconds=60)


def test_live_appends_to_the_live_stream_only(redis, clock):
    """covers: AC-11 (the stream is scoped to TRADING_ENV)"""
    from .conftest import PASSWORD, code_at, live_client

    with live_client(redis, clock) as client:
        client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now)})
        response = client.post("/control/stop", data={"csrf": _csrf(client)}, headers=HTMX)
    assert response.status_code == 200
    assert [call["name"] for call in redis.xadd_calls] == ["live:control"]
    assert STREAM not in redis.streams


def test_a_sent_stop_logs_one_info_line_with_its_id(logged_in, redis, caplog):
    """covers: AC-11"""
    with caplog.at_level(logging.INFO):
        logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)}, headers=HTMX)
    [entry] = _entries(redis)
    sent = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "control_sent"]
    assert len(sent) == 1
    assert sent[0].levelno == logging.INFO
    assert sent[0].fields == {"code": "control_sent", "id": entry["id"], "cmd": "stop"}


def test_a_plain_post_without_a_csrf_field_is_refused_and_appends_nothing(logged_in, redis):
    """covers: AC-11"""
    response = logged_in.post("/control/stop", headers=REFERER)
    assert response.status_code == 403
    assert "Stop NOT sent: reload the page" in response.text
    assert 'class="topbar"' in response.text
    assert _entries(redis) == []


def test_a_hung_redis_fails_closed_like_a_dead_one(logged_in, redis, caplog):
    """covers: AC-13 (no answer within 1 s: 503, ERROR, nothing shown as sent)"""
    csrf = _csrf(logged_in)
    redis.hang = 2.0

    with caplog.at_level(logging.ERROR):
        started = time.monotonic()
        htmx = logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)
        elapsed = time.monotonic() - started
        plain = logged_in.post("/control/stop", data={"csrf": csrf}, headers=REFERER)

    assert elapsed < 1.8  # the send gives up at 1 s, long before the fake would answer
    for response in (htmx, plain):
        assert response.status_code == 503
        assert "Stop NOT sent: Redis did not answer" in response.text
        assert "sent ·" not in response.text
    assert 'class="topbar"' in plain.text
    codes = [r.fields["code"] for r in caplog.records if r.levelno == logging.ERROR]
    assert codes == ["control_send_failed", "control_send_failed"]
    assert _entries(redis) == []


def test_a_failed_stop_never_shows_the_command_as_sent_in_the_top_bar(logged_in, redis):
    """covers: AC-13"""
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")
    logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)
    redis.fail = None
    page = logged_in.get("/").text
    assert "sent ·" not in page
    assert "command--none" in page


# --- critique round 2026-10-03: the alert bar after a stop


def _alert_bar(page):
    match = re.search(r'sse-swap="topbar-alert" role="alert">(.*?)</div>', page, re.S)
    assert match, "no alert bar"
    return match.group(1)


def test_a_failed_stop_raises_the_failed_alert_with_its_time(logged_in, redis, clock):
    """covers: AC-21"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)})
    command_id = _entries(redis)[-1]["id"]
    at = format_utc(datetime.fromtimestamp(clock.now, tz=UTC))
    redis.append(
        "demo:control:results", {"id": command_id, "outcome": "failed", "outcome_at": at, "schema_version": "1"}
    )
    bar = _alert_bar(logged_in.get("/").text)
    assert '<p class="alert alert--bad" data-alert="stop_failed">' in bar
    assert f'<time datetime="{at[:19]}Z"' in bar  # an absolute time through ts, never a ticking count
    assert "Tap Stop again" in bar


def test_a_stop_nobody_handles_raises_the_not_handled_alert_after_its_expiry(logged_in, redis, clock):
    """covers: AC-21"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)})
    redis.values["demo:snapshot:heartbeat"] = encode(clock.ns(), {"process": "trader"})
    assert _alert_bar(logged_in.get("/").text) == ""  # awaiting trader, with a fresh trader, is not an alarm
    clock.now += 301
    bar = _alert_bar(logged_in.get("/").text)
    assert 'data-alert="stop_unhandled"' in bar
    assert "Stop not handled." in bar


def test_the_command_in_the_top_bar_carries_its_age(logged_in, clock):
    """covers: AC-12"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)})
    clock.now += 12
    assert '<span class="command__age">· 12s ago</span>' in logged_in.get("/").text


def test_a_stop_the_running_trader_ignores_raises_the_not_acknowledged_alert(logged_in, redis, clock):
    """covers: AC-21 (after DASHBOARD_ACK_WARN_S with a fresh heartbeat, amber, with the send time)"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)})
    clock.now += 11
    redis.values["demo:snapshot:heartbeat"] = encode(clock.ns(), {"process": "trader"})
    bar = _alert_bar(logged_in.get("/").text)
    assert '<p class="alert alert--warn" data-alert="stop_unacknowledged">' in bar
    assert "Stop not acknowledged." in bar


def test_every_stop_alert_names_the_way_out_on_this_environments_venue(logged_in, redis, clock):
    """covers: AC-21 (close positions on Kraken directly when the bot will not stop)"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in)})
    clock.now += 301
    bar = _alert_bar(logged_in.get("/").text)
    assert (
        '<a href="https://demo-futures.kraken.com/" rel="noopener noreferrer" target="_blank">Kraken Futures</a>' in bar
    )
