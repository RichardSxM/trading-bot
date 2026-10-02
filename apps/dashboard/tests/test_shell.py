"""covers: spec 0004 AC-7, AC-9, AC-18, the bare shell with the live data age, headers, no inline code."""

import re

import pytest
from fastapi.testclient import TestClient

from trading_core.snapshot.envelope import encode

from .conftest import make_app

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@pytest.mark.parametrize(
    ("env", "badge", "favicon"),
    [("demo", "badge--demo", "/static/favicon-demo.svg"), ("live", "badge--live", "/static/favicon-live.svg")],
)
def test_the_shell_shows_the_env_badge_title_prefix_and_favicon(redis, clock, env, badge, favicon):
    redis.values[f"{env}:snapshot:heartbeat"] = encode(clock.ns(), {})
    kwargs = {"dashboard_cookie_secure": True} if env == "live" else {}
    with TestClient(make_app(redis, clock, env=env, **kwargs), follow_redirects=False) as client:
        page = client.get("/login").text
    assert f"<title>{env.upper()} · Log in</title>" in page
    assert badge in page
    assert favicon in page


def test_the_overview_page_shows_the_shell_and_its_empty_state(logged_in):
    page = logged_in.get("/").text
    assert "<title>DEMO · Overview</title>" in page
    assert 'class="topbar"' in page
    assert 'sse-connect="/events"' in page
    assert "Live position and P&amp;L appear here once the trader runs a strategy." in page


@pytest.mark.parametrize(
    ("age_s", "level", "text"),
    [(0, "ok", "0s"), (3, "ok", "3s"), (3.5, "warn", "3s"), (10, "warn", "10s"), (10.5, "bad", "10s")],
)
def test_the_data_age_is_neutral_then_amber_then_red(logged_in, redis, clock, age_s, level, text):
    redis.values["demo:snapshot:heartbeat"] = encode(clock.ns() - int(age_s * 1e9), {})
    page = logged_in.get("/").text
    assert re.search(
        rf'class="age age--{level}"><span class="age__label">trader</span> <span class="num">{text}<', page
    )


def test_missing_and_broken_heartbeats_and_redis_errors_are_named(logged_in, redis):
    del redis.values["demo:snapshot:heartbeat"]
    assert "no heartbeat" in logged_in.get("/").text
    redis.values["demo:snapshot:heartbeat"] = b"{not json"
    assert "bad snapshot" in logged_in.get("/").text
    redis.fail = TimeoutError()
    assert "link lost" in logged_in.get("/").text


def test_every_html_response_carries_the_security_headers(logged_in, redis, clock):
    with TestClient(make_app(redis, clock)) as anonymous:
        responses = [logged_in.get("/"), anonymous.get("/login"), anonymous.post("/login", data={"password": "x"})]
    for response in responses:
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in response.headers


def test_static_files_are_cached_for_a_day(client):
    response = client.get("/static/js/shell.js")
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_no_page_has_inline_script_inline_style_hx_on_or_another_origin(logged_in, redis, clock):
    with TestClient(make_app(redis, clock)) as anonymous:
        pages = {
            "/": logged_in.get("/").text,
            "/styleguide": logged_in.get("/styleguide").text,
            "/login": anonymous.get("/login?stop_lost=1").text,
        }
    for path, page in pages.items():
        assert not re.search(r"<script(?![^>]*\bsrc=)", page), path
        assert "<style" not in page, path
        assert " style=" not in page, path
        assert "hx-on" not in page, path
        # A link the user follows loads nothing; every other URL (scripts, styles, forms, SSE) is ours.
        loads = re.sub(r"<a\b[^>]*>", "", page)
        for url in re.findall(r'(?:src|href|action|sse-connect)="([^"]+)"', loads):
            assert url.startswith("/"), (path, url)
            assert not url.startswith("//"), (path, url)
