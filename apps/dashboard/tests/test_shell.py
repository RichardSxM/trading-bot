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
    assert "Stop, pause, and resume commands appear here as they happen." in page
    assert "Trader offline" in page  # no position snapshot: never shown as flat (spec 0006 AC-17)


@pytest.mark.parametrize(
    ("path", "title", "empty"),
    [
        ("/", "Overview", "Stop, pause, and resume commands appear here as they happen."),
        ("/trades", "Trades", "Fills and closed trades appear here."),
        ("/strategies", "Strategies", "Backtests and strategy scores appear here."),
        ("/system", "System", "Command history and service health appear here."),
    ],
)
def test_every_nav_page_shows_the_shell_its_empty_state_and_marks_itself_current(
    logged_in, redis, clock, path, title, empty
):
    with TestClient(make_app(redis, clock), follow_redirects=False) as anonymous:
        assert anonymous.get(path).status_code == 303
    page = logged_in.get(path).text
    assert f"<title>DEMO · {title}</title>" in page
    assert empty in page
    assert re.search(r'<span class="empty-state__icon"><svg class="icon"', page)  # its line icon (verify step 19)
    assert 'id="stop-area"' in page
    links = re.findall(r'<a class="nav__item[^"]*" href="([^"]+)"', page)
    assert links == ["/", "/trades", "/strategies", "/system"]
    assert re.search(rf'<a class="nav__item is-current" href="{re.escape(path)}" aria-current="page">', page)
    assert page.count('aria-current="page"') == 1


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
            "/trades": logged_in.get("/trades").text,
            "/strategies": logged_in.get("/strategies").text,
            "/system": logged_in.get("/system").text,
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


# --- gaps found against spec 0004 (/test pass)

SHELL_PAGES = ("/", "/trades", "/strategies", "/system", "/styleguide")
TITLES = {"/": "Overview", "/trades": "Trades", "/strategies": "Strategies", "/system": "System"}
TITLES["/styleguide"] = "Styleguide"


def _topbar(page):
    return page[page.index('<header class="topbar">') : page.index("</header>")]


def _csrf(page):
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match
    return match.group(1)


def test_live_shows_the_red_badge_border_class_title_and_favicon_on_every_shell_page(redis, clock):
    """covers: AC-7, AC-16 (the styleguide in live too)"""
    from .conftest import PASSWORD, code_at, live_client

    with live_client(redis, clock) as client:
        assert client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now)}).status_code == 303
        pages = {path: client.get(path) for path in SHELL_PAGES}
    for path, response in pages.items():
        page = response.text
        assert response.status_code == 200, path
        assert f"<title>LIVE · {TITLES[path]}</title>" in page, path
        assert '<body class="env-live">' in page, path  # the 2 px red top border hangs off this class
        assert '<link rel="icon" type="image/svg+xml" href="/static/favicon-live.svg">' in page, path
        assert '<span class="badge badge--live" role="status">LIVE</span>' in _topbar(page), path
        if path != "/styleguide":  # the styleguide samples both badges on purpose
            assert "DEMO" not in page, path


@pytest.mark.parametrize("path", SHELL_PAGES)
def test_demo_shows_the_demo_badge_class_title_and_favicon_on_every_shell_page(logged_in, path):
    """covers: AC-7"""
    page = logged_in.get(path).text
    assert f"<title>DEMO · {TITLES[path]}</title>" in page
    assert '<body class="env-demo">' in page
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon-demo.svg">' in page
    assert '<span class="badge badge--demo" role="status">DEMO</span>' in _topbar(page)
    if path != "/styleguide":  # the styleguide samples both badges on purpose
        assert "LIVE" not in page


def test_every_html_page_carries_exactly_one_env_title_prefix(logged_in, redis, clock):
    """covers: AC-7 (no page renders without one of the two)"""
    from redis.exceptions import ConnectionError as RedisConnectionError

    csrf = _csrf(logged_in.get("/").text)
    pages = [logged_in.get(path).text for path in SHELL_PAGES if path != "/styleguide"]
    pages.append(
        logged_in.post("/control/stop", data={"csrf": "wrong"}, headers={"Referer": "http://testserver/"}).text
    )
    redis.fail = RedisConnectionError("down")
    pages.append(logged_in.post("/control/stop", data={"csrf": csrf}, headers={"Referer": "http://testserver/"}).text)
    with TestClient(make_app(redis, clock)) as anonymous:
        pages.append(anonymous.get("/login").text)
    for page in pages:
        assert len(re.findall(r"<title>(DEMO|LIVE) · ", page)) == 1
        assert len(re.findall(r'class="badge badge--(demo|live)" role="status"', page)) == 1
    styleguide = logged_in.get("/styleguide").text
    assert len(re.findall(r"<title>(DEMO|LIVE) · ", styleguide)) == 1


@pytest.mark.parametrize("path", ["/", "/trades", "/strategies", "/system"])
def test_every_nav_page_has_the_whole_top_bar(logged_in, path):
    """covers: AC-6 (env badge, link status, data age, last command state, stop button)"""
    topbar = _topbar(logged_in.get(path).text)
    assert "badge--demo" in topbar
    assert re.search(r'id="link-status" role="status"', topbar)
    assert re.search(r'<span class="topbar__age" sse-swap="topbar-age"><span class="age age--\w+">', topbar)
    assert re.search(r'id="topbar-command" sse-swap="topbar-command"><span class="command command--\w+">', topbar)
    stop = re.search(r'<form id="stop-area".*?</form>', topbar, re.S)
    assert stop
    assert 'action="/control/stop"' in stop.group(0)
    assert re.search(
        r'<button type="submit" class="button button--stop"[^>]*>.*<span class="button__label">Stop</span>',
        stop.group(0),
    )


def test_the_nav_names_each_page(logged_in):
    """covers: AC-6"""
    page = logged_in.get("/").text
    nav = page[page.index('<nav class="nav" aria-label="Pages">') :]
    nav = nav[: nav.index("</nav>")]
    assert re.findall(r"<span[^>]*>(Overview|Trades|Strategies|System)</span>", nav) == [
        "Overview",
        "Trades",
        "Strategies",
        "System",
    ]


def test_no_data_shows_in_the_data_age_and_command_when_redis_fails(logged_in, redis):
    """covers: AC-10 (Redis unreadable is "no data", never "link lost": the browser's link may be fine)"""
    redis.fail = TimeoutError()
    topbar = _topbar(logged_in.get("/").text)
    assert (
        '<span class="age age--lost"><span class="age__label">trader</span> <span class="num">no data</span>' in topbar
    )
    assert '<span class="command command--lost"><span class="command__label">command</span> no data</span>' in topbar


def test_a_heartbeat_from_the_future_shows_0s_on_the_page(logged_in, redis, clock):
    """covers: AC-9 (clock skew)"""
    redis.values["demo:snapshot:heartbeat"] = encode(clock.ns() + 5_000_000_000, {})
    assert re.search(
        r'class="age age--ok"><span class="age__label">trader</span> <span class="num">0s<', logged_in.get("/").text
    )


def test_the_data_age_thresholds_come_from_settings(redis, clock):
    """covers: AC-9"""
    from .conftest import log_in

    with TestClient(
        make_app(redis, clock, dashboard_stale_amber_s=1, dashboard_stale_red_s=2), follow_redirects=False
    ) as client:
        log_in(client, clock)
        redis.values["demo:snapshot:heartbeat"] = encode(clock.ns() - 1_500_000_000, {})
        amber = client.get("/").text
        redis.values["demo:snapshot:heartbeat"] = encode(clock.ns() - 2_500_000_000, {})
        red = client.get("/").text
    assert 'class="age age--warn"' in amber
    assert 'class="age age--bad"' in red


def _error_pages(logged_in, redis, clock):
    """The HTML replies that are not plain page loads: failed logins, the 429, the stop errors."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    referer = {"Referer": "http://testserver/trades"}
    csrf = _csrf(logged_in.get("/").text)
    replies = {
        "csrf 403 page": logged_in.post("/control/stop", data={"csrf": "wrong"}, headers=referer),
        "csrf 403 partial": logged_in.post("/control/stop", data={"csrf": "wrong"}, headers={"HX-Request": "true"}),
        "sent partial": logged_in.post("/control/stop", data={"csrf": csrf}, headers={"HX-Request": "true"}),
        "overview with activity": logged_in.get("/"),
    }
    redis.fail = RedisConnectionError("down")
    replies["503 page"] = logged_in.post("/control/stop", data={"csrf": csrf}, headers=referer)
    replies["503 partial"] = logged_in.post("/control/stop", data={"csrf": csrf}, headers={"HX-Request": "true"})
    redis.fail = None
    with TestClient(make_app(redis, clock)) as anonymous:
        replies["failed login"] = anonymous.post("/login", data={"password": "x", "code": "000000"})
        for _ in range(4):
            anonymous.post("/login", data={"password": "x", "code": "000000"})
        replies["429 page"] = anonymous.post("/login", data={"password": "x", "code": "000000"})
    assert replies["429 page"].status_code == 429
    return replies


def test_error_pages_and_partials_carry_the_security_headers(logged_in, redis, clock):
    """covers: AC-18"""
    for name, response in _error_pages(logged_in, redis, clock).items():
        assert response.headers["content-security-policy"] == CSP, name
        assert response.headers["x-content-type-options"] == "nosniff", name
        assert response.headers["referrer-policy"] == "same-origin", name
        assert response.headers["cache-control"] == "no-store", name


@pytest.mark.parametrize("path", ["/trades", "/strategies", "/system", "/styleguide"])
def test_every_shell_page_carries_the_security_headers(logged_in, path):
    """covers: AC-18"""
    response = logged_in.get(path)
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_the_event_stream_carries_the_security_headers(logged_in, monkeypatch):
    """covers: AC-18, AC-10 (the stream starts with the current partials)"""
    from dashboard.web import page_routes

    seen = {}

    async def first_partials(poller, initial, *args, **kwargs):
        seen.update(initial)
        for name, html in initial.items():
            yield page_routes.sse_message(name, html)

    # The real stream runs until the session ends, which a fake clock never reaches.
    monkeypatch.setattr(page_routes, "event_stream", first_partials)
    response = logged_in.get("/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-accel-buffering"] == "no"
    assert set(seen) == {
        "topbar-age",
        "topbar-command",
        "topbar-alert",
        "overview-activity",
        "topbar-pause",
        "overview-position",
    }
    assert "event: topbar-age\n" in response.text


def test_error_pages_and_partials_have_no_inline_code_or_other_origin(logged_in, redis, clock):
    """covers: AC-18"""
    logged_in.post("/control/stop", data={"csrf": _csrf(logged_in.get("/").text)}, headers={"HX-Request": "true"})
    for name, response in _error_pages(logged_in, redis, clock).items():
        page = response.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", page), name
        assert "<style" not in page, name
        assert not re.search(r"\sstyle\s*=", page), name
        assert "hx-on" not in page, name
        loads = re.sub(r"<a\b[^>]*>", "", page)
        for url in re.findall(r'(?:src|href|action|sse-connect|hx-post|hx-get)="([^"]+)"', loads):
            assert url.startswith("/"), (name, url)
            assert not url.startswith("//"), (name, url)


@pytest.mark.parametrize("path", SHELL_PAGES)
def test_no_shell_page_has_a_style_attribute_after_any_whitespace(logged_in, path):
    """covers: AC-18 (tabs and newlines too, not only a space)"""
    assert not re.search(r"\sstyle\s*=", logged_in.get(path).text)


def test_static_errors_are_not_cached(client):
    """covers: AC-18 (only a real static file gets the day long cache)"""
    response = client.get("/static/js/missing.js")
    assert response.status_code == 404
    assert response.headers.get("cache-control") != "public, max-age=86400"


# --- accessibility


def test_the_shell_has_a_skip_link_main_landmark_and_named_nav(logged_in):
    """covers: AC-6"""
    page = logged_in.get("/").text
    assert '<a class="skip-link" href="#main">Skip to content</a>' in page
    assert '<main class="page" id="main">' in page
    assert '<nav class="nav" aria-label="Pages">' in page


def test_every_login_input_has_a_label_and_the_right_autocomplete(client):
    """covers: AC-1"""
    page = client.get("/login").text
    for input_id, autocomplete in (("password", "current-password"), ("code", "one-time-code")):
        assert f'<label class="field__label" for="{input_id}">' in page
        tag = re.search(rf'<input[^>]*\bid="{input_id}"[^>]*>', page)
        assert tag, input_id
        assert f'autocomplete="{autocomplete}"' in tag.group(0)
    assert '<main class="login" id="main">' in page


def test_login_errors_are_announced(client):
    """covers: AC-1, AC-2"""
    page = client.post("/login", data={"password": "x", "code": "000000"}).text
    assert '<p class="notice notice--loss" role="alert">Password or code is wrong</p>' in page


def test_a_failed_stop_is_announced(logged_in):
    """covers: AC-11, AC-13"""
    reply = logged_in.post("/control/stop", data={"csrf": "wrong"}, headers={"HX-Request": "true"}).text
    assert '<form id="stop-area" class="stop-area stop-area--failed"' in reply
    assert '<span class="stop-area__error" role="alert">Stop NOT sent: reload the page</span>' in reply
    template = logged_in.get("/").text
    assert '<template id="stop-failed"><span class="stop-area__error" role="alert">' in template


# --- critique round 2026-10-03: the alert bar and "no data"


def _alert_bar(page):
    match = re.search(
        r'<div class="alert-bar" id="alert-bar" sse-swap="topbar-alert" role="alert">(.*?)</div>', page, re.S
    )
    assert match, "no alert bar"
    return match.group(1)


@pytest.mark.parametrize("path", SHELL_PAGES)
def test_every_shell_page_has_an_empty_alert_bar_inside_the_top_bar_when_all_is_well(logged_in, redis, clock, path):
    """covers: AC-21 (inside the sticky top bar, a role=alert region, empty while the trader is fresh)"""
    redis.values["demo:snapshot:heartbeat"] = encode(clock.ns(), {"process": "trader"})
    page = logged_in.get(path).text
    assert 'id="alert-bar"' in _topbar(page)
    assert _alert_bar(page) == ""


def test_a_silent_trader_raises_the_alert_on_the_page(logged_in, redis):
    """covers: AC-21"""
    del redis.values["demo:snapshot:heartbeat"]
    bar = _alert_bar(logged_in.get("/trades").text)
    assert 'data-alert="trader_silent"' in bar
    assert "Trader silent." in bar
    assert "Check that the trader is running." in bar


def test_unreadable_redis_raises_the_no_data_alert(logged_in, redis):
    """covers: AC-21, AC-10"""
    redis.fail = TimeoutError()
    bar = _alert_bar(logged_in.get("/").text)
    assert '<p class="alert alert--warn" data-alert="no_data">' in bar
    assert "can't read Redis" in bar
