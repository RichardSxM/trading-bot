"""covers: spec 0005 AC-13, Pause and Resume on System, the pause in the top bar, and Activity rows;
spec 0010 AC-18 (Resume sends ``resume``, Flatten, the confirm step that works without JavaScript)."""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard.live.state import (
    PAUSE_NO_DATA,
    RUNNING,
    AgeView,
    CommandView,
    PauseView,
    alert_state,
    pause_view,
)
from trading_core.control.messages import format_utc
from trading_core.snapshot.envelope import encode

STREAM = "demo:control"
RESULTS = "demo:control:results"
STATE_KEY = "demo:snapshot:trader_state"
HTMX = {"HX-Request": "true"}
SYSTEM = {"Referer": "http://testserver/system"}
# path, cmd, label, and what a sending POST adds to the csrf (the confirm step's answer).
BUTTONS = [
    ("/control/pause", "pause", "Pause", {}),
    ("/control/resume", "resume", "Resume", {"confirm": "1"}),
    ("/control/flatten", "flatten", "Flatten", {"confirm": "1"}),
]


def _csrf(client):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/system").text)
    assert match
    return match.group(1)


def _entries(redis):
    return [{k.decode(): v.decode() for k, v in fields.items()} for _, fields in redis.streams.get(STREAM, [])]


def _state(clock, **data):
    return encode(clock.ns(), {"paused": False, "open_positions": 0, "open_entry_orders": 0} | data)


# The buttons ------------------------------------------------------------------------------


def test_system_has_pause_and_resume_forms_with_the_csrf_token(logged_in):
    page = logged_in.get("/system").text

    assert 'id="pause-area"' in page
    assert 'action="/control/pause"' in page
    assert 'action="/control/resume"' in page
    assert 'action="/control/flatten"' in page
    assert "/control/unpause" not in page
    assert ">Pause<" in page
    assert ">Resume<" in page
    assert ">Flatten<" in page
    assert page.count('name="csrf"') >= 5  # stop, pause, resume, flatten, log out


@pytest.mark.parametrize(("path", "cmd", "label", "extra"), BUTTONS)
def test_one_tap_appends_one_command_from_the_dashboard(logged_in, redis, path, cmd, label, extra):
    response = logged_in.post(path, data={"csrf": _csrf(logged_in), **extra}, headers=HTMX)

    assert response.status_code == 200
    assert 'id="pause-area"' in response.text
    assert f"{cmd} sent · awaiting trader" in response.text  # the command state, out of band
    [entry] = _entries(redis)
    assert (entry["cmd"], entry["source"]) == (cmd, "dashboard")
    assert uuid.UUID(entry["id"]).version == 4


@pytest.mark.parametrize(("path", "cmd", "label", "extra"), BUTTONS)
def test_without_javascript_the_form_post_returns_to_system(logged_in, redis, path, cmd, label, extra):
    response = logged_in.post(path, data={"csrf": _csrf(logged_in), **extra}, headers=SYSTEM)

    assert response.status_code == 303
    assert response.headers["location"] == "/system"
    assert len(_entries(redis)) == 1


@pytest.mark.parametrize(("path", "cmd", "label", "extra"), BUTTONS)
def test_a_missing_or_wrong_csrf_token_is_refused_and_appends_nothing(logged_in, redis, path, cmd, label, extra):
    missing = logged_in.post(path, data=extra, headers=HTMX)
    wrong = logged_in.post(path, data={"csrf": "wrong", **extra}, headers=HTMX)
    plain = logged_in.post(path, data={"csrf": "wrong", **extra}, headers=SYSTEM)

    for response in (missing, wrong, plain):
        assert response.status_code == 403
        assert f"{label} NOT sent: reload the page" in response.text
        assert "Stop NOT sent: reload the page" not in response.text
    assert 'id="pause-area"' in plain.text  # System again, without JavaScript
    assert _entries(redis) == []


@pytest.mark.parametrize(("path", "cmd", "label", "extra"), BUTTONS)
def test_redis_down_answers_503_logs_error_and_never_says_sent(logged_in, redis, caplog, path, cmd, label, extra):
    csrf = _csrf(logged_in)
    redis.fail = RedisConnectionError("down")

    with caplog.at_level(logging.ERROR):
        htmx = logged_in.post(path, data={"csrf": csrf, **extra}, headers=HTMX)
        plain = logged_in.post(path, data={"csrf": csrf, **extra}, headers=SYSTEM)

    for response in (htmx, plain):
        assert response.status_code == 503
        assert f"{label} NOT sent: Redis did not answer" in response.text
        assert "sent ·" not in response.text
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.fields["code"] for r in errors] == ["control_send_failed", "control_send_failed"]
    assert {r.fields["cmd"] for r in errors} == {cmd}


@pytest.mark.parametrize(
    ("path", "lost", "notice"),
    [
        ("/control/pause", "pause", "Your pause was NOT sent. Log in and tap Pause again."),
        ("/control/resume", "resume", "Your resume was NOT sent. Log in and tap Resume again."),
        ("/control/flatten", "flatten", "Your flatten was NOT sent. Log in and tap Flatten again."),
    ],
)
def test_a_tap_without_a_session_goes_to_login_saying_it_was_not_sent(client, redis, path, lost, notice):
    htmx = client.post(path, headers={**HTMX, **SYSTEM})
    plain = client.post(path, headers=SYSTEM)

    target = f"/login?next=%2Fsystem&lost={lost}"
    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"] == target
    assert plain.status_code == 303
    assert plain.headers["location"] == target
    assert notice in client.get(target).text
    assert _entries(redis) == []


@pytest.mark.parametrize(("path", "label"), [("/control/resume", "resume"), ("/control/flatten", "flatten")])
def test_the_first_tap_asks_to_confirm_and_sends_nothing(logged_in, redis, path, label):
    csrf = _csrf(logged_in)

    htmx = logged_in.post(path, data={"csrf": csrf}, headers=HTMX)
    plain = logged_in.post(path, data={"csrf": csrf}, headers=SYSTEM)

    for response in (htmx, plain):
        assert response.status_code == 200
        assert f">Confirm {label}<" in response.text
        assert 'name="confirm" value="1"' in response.text
        assert 'href="/system"' in response.text  # Cancel, a plain link back
        assert "sent ·" not in response.text
    assert 'id="pause-area"' in htmx.text
    assert "<html" in plain.text  # System again, with the area asking, without JavaScript
    assert _entries(redis) == []


def test_only_confirm_1_sends(logged_in, redis):
    csrf = _csrf(logged_in)

    logged_in.post("/control/resume", data={"csrf": csrf, "confirm": "yes"}, headers=HTMX)
    assert _entries(redis) == []
    logged_in.post("/control/resume", data={"csrf": csrf, "confirm": "1"}, headers=HTMX)
    assert [e["cmd"] for e in _entries(redis)] == ["resume"]


def test_the_unpause_route_is_gone_from_the_dashboard(logged_in):
    assert logged_in.post("/control/unpause", data={"csrf": _csrf(logged_in)}, headers=HTMX).status_code in (404, 405)


def test_an_unknown_lost_value_shows_no_notice(client):
    assert "NOT sent" not in client.get("/login?next=%2F&lost=launch").text


# The top bar ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ({"paused": True, "pause_source": "deploy"}, "paused · by deploy"),
        ({"paused": True, "pause_source": "dashboard"}, "paused · by you"),
    ],
)
def test_the_top_bar_names_who_paused(logged_in, redis, clock, data, text):
    redis.values[STATE_KEY] = _state(clock, **data)

    assert text in logged_in.get("/").text


def test_the_top_bar_shows_nothing_while_the_trader_runs(logged_in, redis, clock):
    redis.values[STATE_KEY] = _state(clock)

    page = logged_in.get("/").text
    assert "paused ·" not in page
    assert '<span class="topbar__pause" sse-swap="topbar-pause"></span>' in page


def test_the_top_bar_says_no_data_without_trader_state(logged_in, redis):
    redis.values.pop(STATE_KEY, None)

    assert re.search(r'class="pause pause--lost">.*no data', logged_in.get("/").text)


def test_pause_view_states():
    assert pause_view(None) is PAUSE_NO_DATA
    assert pause_view("nope") == PauseView("bad snapshot", "bad")
    assert pause_view(encode(1, {"paused": False})) is RUNNING
    assert pause_view(encode(1, {"paused": True, "pause_source": "deploy"})) == PauseView("paused · by deploy", "warn")
    assert pause_view(encode(1, {"paused": True, "pause_source": None})) == PauseView("paused", "warn")


# Alerts and Activity ----------------------------------------------------------------------


def test_a_failed_pause_shows_in_the_top_bar_but_raises_no_stop_alert():
    now = datetime(2026, 10, 3, tzinfo=UTC)
    failed_pause = CommandView("pause sent · failed", "bad", final=True, status="failed", since=now, cmd="pause")
    failed_stop = CommandView("stop sent · failed", "bad", final=True, status="failed", since=now, cmd="stop")
    awaiting_unpause = CommandView(
        "unpause sent · awaiting trader", "pending", status="awaiting", since=now, cmd="unpause"
    )
    fresh = AgeView("1s", "ok")

    assert alert_state(fresh, failed_pause, now, 10) is None
    stop_alert = alert_state(fresh, failed_stop, now, 10)
    assert stop_alert is not None
    assert stop_alert.kind == "stop_failed"
    later = now.replace(minute=5)
    assert alert_state(fresh, awaiting_unpause, later, 10) is None


def test_activity_lists_pause_and_resume_next_to_stop(logged_in, redis, clock):
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    csrf = _csrf(logged_in)
    logged_in.post("/control/stop", data={"csrf": csrf}, headers=HTMX)
    logged_in.post("/control/pause", data={"csrf": csrf}, headers=HTMX)
    logged_in.post("/control/resume", data={"csrf": csrf, "confirm": "1"}, headers=HTMX)
    pause_id = _entries(redis)[1]["id"]
    redis.append(RESULTS, {"id": pause_id, "outcome": "executed", "outcome_at": format_utc(now), "schema_version": "1"})

    feed = logged_in.get("/").text
    assert re.search(r"resume.*awaiting trader", feed, re.S)
    assert re.search(r"pause.*confirmed", feed, re.S)
    assert "stop" in feed


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("not_flat", "resume sent · failed · not flat yet"),
        ("total_loss_limit", "resume sent · failed · total loss limit"),
        ("loss_unknown", "resume sent · failed · loss not known yet"),
        ("halt_key_delete_failed", "resume sent · failed · halt not cleared"),
        ("flatten_timeout", "resume sent · failed · flatten timeout"),
        ("halt_key_write_failed", "resume sent · failed · halt not saved, a restart would run"),
        ("brand_new", "resume sent · failed · brand_new"),
    ],
)
def test_a_failed_result_shows_its_reason_in_plain_words(reason, words):
    """covers: spec 0010 AC-18 (reasons)"""
    from dashboard.live.state import command_state
    from trading_core.control.messages import ControlResult, command, result_to_fields, to_fields
    from trading_core.recording.enums import ControlCmd, ControlOutcome

    now = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
    msg = command(ControlCmd.RESUME, now, 60)
    result = ControlResult(msg.id, ControlOutcome.FAILED, now, reason)

    view = command_state(to_fields(msg), result_to_fields(result), now, 60)

    assert view.text == words


# spec 0010 AC-16, AC-17: the HALTED badge and the loss limits on System ----------------------


def test_the_top_bar_shows_halted_with_its_reason_and_age_over_a_pause():
    now = datetime(2026, 10, 12, 9, 42, tzinfo=UTC)
    data = {"paused": True, "pause_source": "deploy", "halted": True, "halt_reason": "daily_loss_limit"}
    raw = encode(1, data | {"halted_at": "2026-10-12T09:30:00.000000Z"})

    assert pause_view(raw, now) == PauseView("HALTED · daily loss limit · 12m", "bad")
    assert pause_view(encode(1, {"halted": True, "halt_reason": "new_thing"}), now).text == "HALTED · new_thing"


def test_every_page_shows_the_halted_badge(logged_in, redis, clock):
    redis.values[STATE_KEY] = _state(clock, halted=True, halt_reason="manual_stop", halted_at=None)

    for page in ("/", "/system", "/trades"):
        assert "HALTED · manual stop" in logged_in.get(page).text


def test_system_shows_the_loss_figures_against_their_limits(logged_in, redis, clock):
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    until = format_utc(now + timedelta(minutes=8))
    redis.values[STATE_KEY] = _state(
        clock,
        loss_today_usd="12.50",
        daily_loss_limit_usd="20.00",
        loss_total_usd="-3.25",
        total_loss_limit_usd="100.00",
        trades_today=4,
        max_trades_per_day=50,
        cooldown_until=until,
    )

    page = logged_in.get("/system").text

    assert re.search(r"Loss today</dt><dd>12\.50 of 20\.00 USD", page)
    assert re.search(r"Total loss</dt><dd>\u22123\.25 of 100\.00 USD", page)
    assert re.search(r"Trades today</dt><dd>4 of 50", page)
    assert re.search(r"Cooldown</dt><dd>[78]m left", page)


def test_loss_figures_not_seeded_read_unknown(logged_in, redis, clock):
    redis.values[STATE_KEY] = _state(
        clock, loss_today_usd=None, daily_loss_limit_usd="20.00", trades_today=None, max_trades_per_day=50
    )

    page = logged_in.get("/system").text

    assert re.search(r'Loss today</dt><dd><span class="losses__unknown">unknown</span> of 20\.00 USD', page)
    assert "Cooldown</dt><dd>none" in page


def test_without_trader_state_the_loss_box_says_no_data(logged_in, redis):
    redis.values.pop(STATE_KEY, None)

    assert '<p class="losses__lost">no data</p>' in logged_in.get("/system").text
