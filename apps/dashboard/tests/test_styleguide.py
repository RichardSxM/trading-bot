"""covers: spec 0004 AC-16, /styleguide renders every token and every component state DESIGN.md lists."""

import re

from fastapi.testclient import TestClient

from dashboard.ui.tokens import color_tokens, read_tokens

from .conftest import make_app
from .test_design_tokens import design_md


def listed_states():
    """(macro, state) for every ``### Name (`macro`)`` section with a ``**States:**`` line in DESIGN.md."""
    pairs = []
    for section in re.split(r"^### ", design_md().read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]:
        macro = re.match(r"[^\n(]*\(`(\w+)`\)", section)
        states = re.search(r"\*\*States:\*\*(.*)", section)
        if macro and states:
            pairs += [(macro.group(1), state) for state in re.findall(r"`(\w+)`", states.group(1))]
    return pairs


def test_design_md_lists_the_component_states_the_spec_names():
    pairs = set(listed_states())
    for macro in ("num", "pnl", "ts", "badge", "status_dot", "stat", "table", "button", "stale", "chart"):
        assert any(m == macro for m, _ in pairs), macro
    assert {("button", s) for s in ("default", "danger", "stop", "disabled", "busy")} <= pairs
    assert {("pnl", "profit"), ("pnl", "loss"), ("pnl", "zero"), ("badge", "demo"), ("badge", "live")} <= pairs
    assert {("stat", "stale"), ("stat", "error"), ("stat", "loading"), ("table", "empty")} <= pairs


def test_the_styleguide_needs_a_session(client):
    response = client.get("/styleguide")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fstyleguide"


def test_the_styleguide_renders_every_listed_state_and_every_token(logged_in):
    page = logged_in.get("/styleguide").text
    missing = [f"{m}:{s}" for m, s in listed_states() if f'data-sample="{m}:{s}"' not in page]
    assert missing == []
    for name in color_tokens(read_tokens()):
        assert f'data-sample="token:{name}"' in page
        assert f"sg-chip--{name}" in page


def test_the_styleguide_shows_the_number_rules_and_a_chart(logged_in):
    page = logged_in.get("/styleguide").text
    assert "1,234.50" in page
    assert ">1,000<" in page
    assert 'class="pnl pnl--zero">0.00<' in page
    assert 'title="16:03:07 CEST, Amsterdam">2026-10-02 14:03:07Z</time>' in page
    assert "data-chart" in page
    assert '"close": "' in page.replace("&#34;", '"')  # prices travel as strings, never floats
    assert 'src="/static/vendor/lightweight-charts.standalone.production.js"' in page
    assert "Charting by TradingView" in page
    assert "All values on this page are samples" in page


def test_the_chart_script_turns_the_library_logo_off(redis, clock):
    with TestClient(make_app(redis, clock)) as client:
        script = client.get("/static/js/chart.js").text
    assert "attributionLogo: false" in script


def test_only_the_styleguide_loads_the_chart_library(logged_in):
    assert "lightweight-charts" not in logged_in.get("/").text
