"""covers: spec 0004 AC-11, AC-12, AC-13, the stop button and the command state in the top bar."""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.control.messages import format_utc

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
