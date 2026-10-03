"""covers: spec 0004 AC-1 to AC-4 and AC-18 through the real app (httpx TestClient, fake Redis)."""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from .conftest import PASSWORD, code_at, log_in, make_app

# --- AC-1: no session


def test_a_page_without_a_session_redirects_to_login_with_next(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_an_htmx_request_without_a_session_gets_401_and_hx_redirect(client):
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2F"


def test_events_without_a_session_answers_204_so_eventsource_stops(client):
    assert client.get("/events").status_code == 204


def test_public_paths_need_no_session(client):
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/css/tokens.css").status_code == 200


def test_a_post_without_a_session_goes_back_to_the_referring_page_after_login(client):
    response = client.post("/logout", headers={"Referer": "http://testserver/trades"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Ftrades"


def test_login_page_shows_the_lost_stop_message(client):
    assert "Your stop was NOT sent. Log in and tap stop again." in client.get("/login?stop_lost=1").text
    assert "NOT sent" not in client.get("/login").text


def test_a_correct_login_sets_a_strict_http_only_cookie_and_redirects_to_next(redis, clock):
    with TestClient(make_app(redis, clock, dashboard_cookie_secure=True), follow_redirects=False) as client:
        response = log_in(client, clock, "/trades")
    assert response.status_code == 303
    assert response.headers["location"] == "/trades"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("kfs_session=")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert flag in cookie


@pytest.mark.parametrize("next_path", ["//evil.example", "https://evil.example", "/login", "/a?b"])
def test_an_unsafe_next_lands_on_root(client, clock, next_path):
    response = log_in(client, clock, next_path)
    assert response.headers["location"] == "/"


def test_the_login_page_with_a_session_redirects_to_root(logged_in):
    response = logged_in.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_failed_login_answers_200_with_the_form_and_keeps_next(client, clock):
    response = client.post("/login", data={"password": "nope", "code": code_at(clock.now), "next": "/trades"})
    assert response.status_code == 200
    assert "Password or code is wrong" in response.text
    assert 'name="next" value="/trades"' in response.text
    assert "set-cookie" not in response.headers


# --- AC-2 and AC-3: failures, lockout, replay


def _failures(caplog):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "login_failed"]


@pytest.mark.parametrize(
    ("password", "code_offset", "reason"),
    [("nope", 0, "bad_password"), (PASSWORD, 5, "bad_code"), ("nope", 5, "bad_password")],
)
def test_each_failure_logs_one_warning_with_reason_and_client_never_the_values(
    client, clock, caplog, password, code_offset, reason
):
    code = code_at(clock.now, code_offset)
    with caplog.at_level(logging.WARNING):
        client.post("/login", data={"password": password, "code": code})
    (record,) = _failures(caplog)
    assert record.levelno == logging.WARNING
    assert record.fields == {"code": "login_failed", "reason": reason, "client": "testclient"}
    assert password not in caplog.text
    assert code not in caplog.text


def test_a_reused_code_is_refused_as_replayed(client, clock, caplog):
    code = code_at(clock.now)
    assert client.post("/login", data={"password": PASSWORD, "code": code}).status_code == 303
    client.cookies.clear()
    with caplog.at_level(logging.WARNING):
        response = client.post("/login", data={"password": PASSWORD, "code": code})
    assert response.status_code == 200
    assert _failures(caplog)[0].fields["reason"] == "replayed_code"


def test_an_older_step_after_a_newer_one_is_a_replay(client, clock):
    assert client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now, 1)}).status_code == 303
    client.cookies.clear()
    assert client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now)}).status_code == 200


def test_a_code_with_a_wrong_password_does_not_burn_the_step(client, clock):
    code = code_at(clock.now)
    client.post("/login", data={"password": "nope", "code": code})
    assert client.post("/login", data={"password": PASSWORD, "code": code}).status_code == 303


def test_five_failures_lock_the_client_even_with_correct_credentials_while_a_session_keeps_working(
    redis, clock, caplog
):
    with TestClient(make_app(redis, clock), follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        session_cookie = client.cookies.get("kfs_session")
        client.cookies.clear()
        clock.now += 30
        for _ in range(5):
            assert client.post("/login", data={"password": "nope", "code": "000000"}).status_code == 200
        with caplog.at_level(logging.WARNING):
            locked = log_in(client, clock)
        assert locked.status_code == 429
        assert _failures(caplog)[-1].fields["reason"] == "locked"
        # The session that already existed still works.
        client.cookies.set("kfs_session", session_cookie)
        assert client.get("/").status_code == 200
        client.cookies.clear()
        clock.now += 15 * 60
        assert log_in(client, clock).status_code == 303


def test_a_burst_of_logins_runs_one_password_check_at_a_time_and_stops_at_the_lockout(redis, clock, monkeypatch):
    app = make_app(redis, clock)
    running, peak, checks = 0, 0, 0
    counter = threading.Lock()

    def slow_verify(password):
        nonlocal running, peak, checks
        with counter:
            running += 1
            checks += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with counter:
            running -= 1
        return False

    monkeypatch.setattr(app.state.context.passwords, "verify", slow_verify)
    with TestClient(app, follow_redirects=False) as client, ThreadPoolExecutor(10) as pool:
        statuses = list(
            pool.map(
                lambda _: client.post("/login", data={"password": "nope", "code": "000000"}).status_code, range(10)
            )
        )

    assert peak == 1
    assert checks == 5
    assert sorted(statuses) == [200] * 5 + [429] * 5


def test_the_lockout_key_behind_tailscale_serve_is_the_tailscale_login():
    from starlette.requests import Request

    from dashboard.web.auth_routes import lockout_key

    def request(peer, headers):
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "client": (peer, 1234), "headers": raw})

    assert lockout_key(request("127.0.0.1", {"Tailscale-User-Login": "me@example.com"})) == "me@example.com"
    assert lockout_key(request("127.0.0.1", {"X-Forwarded-For": "100.64.0.7, 10.0.0.1"})) == "100.64.0.7"
    assert lockout_key(request("::1", {})) == "::1"
    # Without a trusted peer only loopback may set them.
    assert lockout_key(request("172.18.0.1", {"Tailscale-User-Login": "me@example.com"})) == "172.18.0.1"


def test_the_lockout_key_behind_dockers_published_port_trusts_only_the_gateway():
    from starlette.requests import Request

    from dashboard.web.auth_routes import lockout_key

    def request(peer, headers):
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "client": (peer, 1234), "headers": raw})

    gateway = frozenset({"172.18.0.1"})
    assert lockout_key(request("172.18.0.1", {"Tailscale-User-Login": "me@example.com"}), gateway) == "me@example.com"
    assert lockout_key(request("172.18.0.1", {"X-Forwarded-For": "100.64.0.7, 10.0.0.1"}), gateway) == "100.64.0.7"
    assert lockout_key(request("172.18.0.1", {}), gateway) == "172.18.0.1"
    # Another container on the network is not trusted to name a client.
    assert lockout_key(request("172.18.0.5", {"Tailscale-User-Login": "me@example.com"}), gateway) == "172.18.0.5"


def test_trusted_peers_reads_the_default_gateway(tmp_path):
    from dashboard.web.app import trusted_peers

    table = tmp_path / "route"
    table.write_text(ROUTE_TABLE)
    assert trusted_peers(table) == frozenset({"172.18.0.1"})
    assert trusted_peers(tmp_path / "missing") == frozenset()


ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


# --- AC-4: session lifetime and logout


def test_every_page_response_slides_the_session(logged_in, clock):
    first = logged_in.cookies.get("kfs_session")
    clock.now += 60
    response = logged_in.get("/")
    assert "kfs_session=" in response.headers["set-cookie"]
    assert logged_in.cookies.get("kfs_session") != first


def test_a_session_idle_for_24_hours_ends(logged_in, clock):
    clock.now += 24 * 3600
    assert logged_in.get("/").status_code == 303


def test_activity_cannot_keep_a_session_past_7_days(logged_in, clock):
    for _ in range(8):
        clock.now += 23 * 3600
        response = logged_in.get("/")
    assert response.status_code == 303


def test_the_session_end_cue_shows_within_the_last_hour(redis, clock):
    with TestClient(make_app(redis, clock, dashboard_session_max_d=1), follow_redirects=False) as client:
        log_in(client, clock)
        assert "session ends" not in client.get("/").text
        clock.now += 23.5 * 3600
        text = client.get("/").text
    topbar = text[text.index('<header class="topbar">') : text.index("</header>")]
    assert re.search(r"session ends \d\d:\d\dZ", topbar)
    assert text.count("session ends") == 1


def test_a_tampered_cookie_is_no_session(logged_in):
    cookie = logged_in.cookies.get("kfs_session")
    logged_in.cookies.set("kfs_session", cookie[:-2] + "xx")
    assert logged_in.get("/").status_code == 303


def _csrf(client):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert match is not None
    return match.group(1)


def test_logout_needs_the_csrf_token_and_clears_the_cookie(logged_in):
    assert logged_in.post("/logout").status_code == 403
    assert logged_in.post("/logout", data={"csrf": "wrong"}).status_code == 403
    csrf = _csrf(logged_in)
    response = logged_in.post("/logout", data={"csrf": csrf})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "kfs_session=;" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert logged_in.get("/").status_code == 303


def test_logout_accepts_the_csrf_header_from_htmx(logged_in):
    csrf = _csrf(logged_in)
    assert logged_in.post("/logout", headers={"X-CSRF-Token": csrf}).status_code == 303


# --- gaps found against spec 0004 (/test pass)


def _new_app_with(redis, clock, **secrets):
    from pydantic import SecretStr

    from dashboard.settings import DashboardSettings
    from dashboard.web.app import create_app

    from .conftest import SECRETS

    replaced = {**SECRETS, **{name: SecretStr(value) for name, value in secrets.items()}}
    settings = DashboardSettings(trading_env="demo", dashboard_cookie_secure=False)
    return create_app(settings, replaced, redis=redis, clock=clock, clock_ns=clock.ns)


def test_the_page_redirect_names_the_page_itself_in_next(client):
    """covers: AC-1"""
    for path in ("/trades", "/strategies", "/system", "/styleguide"):
        assert client.get(path).headers["location"] == f"/login?next=%2F{path[1:]}"


@pytest.mark.parametrize("headers", [{}, {"Referer": "not a url at all"}, {"Referer": "http://testserver/login"}])
def test_a_post_without_a_usable_referer_goes_back_to_root_after_login(client, headers):
    """covers: AC-1"""
    response = client.post("/logout", headers=headers)
    assert response.headers["location"] == "/login?next=%2F"


def test_a_referer_from_another_host_keeps_only_its_path_never_the_host(client):
    """covers: AC-1"""
    response = client.post("/logout", headers={"Referer": "https://evil.example/trades"})
    assert response.headers["location"] == "/login?next=%2Ftrades"


@pytest.mark.parametrize("next_path", ["/\\evil.example", "/a.b", "/a b", "/a#b", "/café", "/%2F%2Fevil", "trades"])
def test_a_next_with_any_character_outside_the_safe_set_lands_on_root(client, clock, next_path):
    """covers: AC-1"""
    assert log_in(client, clock, next_path).headers["location"] == "/"


def test_an_unsafe_next_on_the_login_form_becomes_root_in_the_hidden_field(client):
    """covers: AC-1"""
    text = client.get("/login", params={"next": "//evil.example"}).text
    assert 'name="next" value="/"' in text
    assert "evil.example" not in text


def test_a_bad_or_replayed_code_also_keeps_next_and_shows_the_one_generic_message(client, clock):
    """covers: AC-1, AC-2"""
    bad = client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now, 5), "next": "/system"})
    assert bad.status_code == 200
    assert "Password or code is wrong" in bad.text
    assert 'name="next" value="/system"' in bad.text

    code = code_at(clock.now)
    assert client.post("/login", data={"password": PASSWORD, "code": code}).status_code == 303
    client.cookies.clear()
    replayed = client.post("/login", data={"password": PASSWORD, "code": code, "next": "/system"})
    assert replayed.status_code == 200
    assert "Password or code is wrong" in replayed.text
    assert 'name="next" value="/system"' in replayed.text


def test_the_lockout_page_keeps_next_and_sets_no_cookie(client, clock):
    """covers: AC-1, AC-2"""
    for _ in range(5):
        client.post("/login", data={"password": "nope", "code": "000000"})
    locked = client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now), "next": "/trades"})
    assert locked.status_code == 429
    assert 'name="next" value="/trades"' in locked.text
    assert "set-cookie" not in locked.headers


def test_a_failure_with_an_unsafe_next_shows_root_in_the_form(client, clock):
    """covers: AC-1"""
    response = client.post("/login", data={"password": "nope", "code": "000000", "next": "//evil.example"})
    assert 'name="next" value="/"' in response.text


def test_locked_attempts_do_not_extend_the_window(client, clock):
    """covers: AC-2 (a 429 attempt is not counted and does not move the window)"""
    start = clock.now
    for _ in range(5):
        client.post("/login", data={"password": "nope", "code": "000000"})
    for minutes in (5, 10, 14):
        clock.now = start + minutes * 60
        assert client.post("/login", data={"password": "nope", "code": "000000"}).status_code == 429
    clock.now = start + 15 * 60
    assert log_in(client, clock).status_code == 303


def test_a_success_through_the_app_resets_the_clients_count(client, clock):
    """covers: AC-2"""
    for _ in range(4):
        client.post("/login", data={"password": "nope", "code": "000000"})
    assert log_in(client, clock).status_code == 303
    client.cookies.clear()
    clock.now += 30
    for _ in range(4):
        assert client.post("/login", data={"password": "nope", "code": "000000"}).status_code == 200
    assert log_in(client, clock).status_code == 303


def test_a_restart_clears_the_counters(redis, clock):
    """covers: AC-2"""
    with TestClient(make_app(redis, clock), follow_redirects=False) as first:
        for _ in range(5):
            first.post("/login", data={"password": "nope", "code": "000000"})
        assert log_in(first, clock).status_code == 429
    with TestClient(make_app(redis, clock), follow_redirects=False) as restarted:
        assert log_in(restarted, clock).status_code == 303


@pytest.mark.parametrize("reason", ["replayed_code", "locked"])
def test_replayed_and_locked_attempts_log_one_record_without_the_values(client, clock, caplog, reason):
    """covers: AC-2"""
    code = code_at(clock.now)
    if reason == "replayed_code":
        assert client.post("/login", data={"password": PASSWORD, "code": code}).status_code == 303
        client.cookies.clear()
    else:
        for _ in range(5):
            client.post("/login", data={"password": "nope", "code": "000000"})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        client.post("/login", data={"password": PASSWORD, "code": code})
    (record,) = _failures(caplog)
    assert record.fields == {"code": "login_failed", "reason": reason, "client": "testclient"}
    assert PASSWORD not in caplog.text
    assert code not in caplog.text


def test_both_factors_are_checked_on_every_attempt(redis, clock, monkeypatch):
    """covers: AC-3 (no early exit that reveals which factor failed)"""
    app = make_app(redis, clock)
    context = app.state.context
    calls = {"verify": 0, "matched_step": 0}
    real_verify, real_match = context.passwords.verify, context.totp.matched_step

    def verify(password):
        calls["verify"] += 1
        return real_verify(password)

    def matched_step(code, now):
        calls["matched_step"] += 1
        return real_match(code, now)

    monkeypatch.setattr(context.passwords, "verify", verify)
    monkeypatch.setattr(context.totp, "matched_step", matched_step)
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"password": "nope", "code": code_at(clock.now)})
        assert calls == {"verify": 1, "matched_step": 1}
        client.post("/login", data={"password": PASSWORD, "code": "000000"})
        assert calls == {"verify": 2, "matched_step": 2}


def test_a_code_from_the_previous_step_logs_in(client, clock):
    """covers: AC-3"""
    assert client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now, -1)}).status_code == 303


def test_an_older_step_after_a_newer_one_is_refused_as_replayed_code(client, clock, caplog):
    """covers: AC-3"""
    assert client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now, 1)}).status_code == 303
    client.cookies.clear()
    with caplog.at_level(logging.WARNING):
        client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now)})
    assert _failures(caplog)[0].fields["reason"] == "replayed_code"


def test_spaces_around_the_code_are_ignored(client, clock):
    """covers: AC-3"""
    response = client.post("/login", data={"password": PASSWORD, "code": f"  {code_at(clock.now)} "})
    assert response.status_code == 303


def test_an_open_event_stream_does_not_slide_the_session(redis, clock, monkeypatch):
    """covers: AC-4 (an open SSE stream does not count as a request)"""
    from dashboard.web import page_routes

    async def one_message(*args, **kwargs):
        yield "event: ping\ndata: \n\n"

    # The real stream runs until the session ends, which a fake clock never reaches.
    monkeypatch.setattr(page_routes, "event_stream", one_message)
    app = make_app(redis, clock)
    with TestClient(app, follow_redirects=False) as client:
        log_in(client, clock)
        cookie = client.cookies.get("kfs_session")
        clock.now += 60
        with client.stream("GET", "/events") as response:
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
        session = app.state.context.codec.decode(cookie, clock.now)
        assert client.cookies.get("kfs_session") == cookie
        assert session is not None
        # Only the stream for 24 h after the last page: the session ends.
        clock.now = session.seen + 24 * 3600
        assert client.get("/").status_code == 303


def test_activity_keeps_a_session_alive_until_the_7_day_cap(logged_in, clock):
    """covers: AC-4 (sliding 24 h idle limit, then the hard cap)"""
    login_at = clock.now - 30
    statuses = []
    while clock.now + 23 * 3600 < login_at + 7 * 86400:
        clock.now += 23 * 3600
        statuses.append(logged_in.get("/").status_code)
    assert statuses
    assert set(statuses) == {200}
    clock.now = login_at + 7 * 86400
    assert logged_in.get("/").status_code == 303


def test_the_session_end_cue_names_the_end_time(redis, clock):
    """covers: AC-4"""
    from datetime import UTC, datetime

    with TestClient(make_app(redis, clock, dashboard_session_max_d=1), follow_redirects=False) as client:
        log_in(client, clock)
        login_at = int(clock.now)
        clock.now = login_at + 23 * 3600 + 1  # 1 h minus 1 s left
        text = client.get("/").text
    expected = datetime.fromtimestamp(login_at + 86400, tz=UTC).strftime("%H:%MZ")
    assert f"session ends {expected}" in text


def test_the_session_end_cue_follows_the_idle_limit_too(redis, clock):
    """covers: AC-4 (the cue shows whichever limit ends the session first)"""
    with TestClient(make_app(redis, clock, dashboard_session_idle_h=0.5), follow_redirects=False) as client:
        log_in(client, clock)
        assert "session ends" in client.get("/").text


def test_a_new_session_key_ends_every_session(redis, clock):
    """covers: AC-4"""
    with TestClient(make_app(redis, clock), follow_redirects=False) as old:
        log_in(old, clock)
        cookie = old.cookies.get("kfs_session")
    with TestClient(_new_app_with(redis, clock, dashboard_session_key="z" * 48), follow_redirects=False) as new:
        new.cookies.set("kfs_session", cookie)
        assert new.get("/").status_code == 303


def test_a_new_password_hash_ends_every_session(redis, clock):
    """covers: AC-4 (the cookie carries the hash fingerprint)"""
    from argon2 import PasswordHasher

    new_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with TestClient(make_app(redis, clock), follow_redirects=False) as old:
        log_in(old, clock)
        cookie = old.cookies.get("kfs_session")
    with TestClient(_new_app_with(redis, clock, dashboard_password_hash=new_hash), follow_redirects=False) as new:
        new.cookies.set("kfs_session", cookie)
        assert new.get("/").status_code == 303


def test_login_sends_one_session_cookie_whose_max_age_is_the_idle_limit(client, clock):
    """covers: AC-4"""
    response = log_in(client, clock)
    cookies = [v for v in response.headers.get_list("set-cookie") if v.startswith("kfs_session=")]
    assert len(cookies) == 1
    match = re.search(r"Max-Age=(\d+)", cookies[0])
    assert match
    max_age = int(match.group(1))
    assert 86399 <= max_age <= 86400  # login time is truncated to whole seconds
    assert "Secure" not in cookies[0]  # this test app runs with DASHBOARD_COOKIE_SECURE=false


def test_logout_sends_only_the_clearing_cookie(logged_in):
    """covers: AC-4 (the handler's cookie wins over the sliding refresh)"""
    response = logged_in.post("/logout", data={"csrf": _csrf(logged_in)})
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert cookies[0].startswith("kfs_session=;")
