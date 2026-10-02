"""covers: spec 0004 AC-1 to AC-4 and AC-18 through the real app (httpx TestClient, fake Redis)."""

import logging
import re

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
