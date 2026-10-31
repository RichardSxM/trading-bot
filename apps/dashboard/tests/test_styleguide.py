"""covers: spec 0004 AC-16, /styleguide renders every token and every component state DESIGN.md lists."""

import itertools
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


# --- gaps found against spec 0004 (/test pass)


def _sample(page, marker):
    """The markup from one ``data-sample`` marker up to the next one."""
    start = page.index(f'data-sample="{marker}"')
    end = page.find('data-sample="', start + 1)
    return page[start : end if end != -1 else len(page)]


def test_each_state_sample_holds_markup_in_that_state(logged_in):
    """covers: AC-16 (the marker wraps the real state, not just a label)"""
    page = logged_in.get("/styleguide").text
    assert 'class="badge badge--live">live<' in _sample(page, "badge:live")
    assert 'class="badge badge--demo">demo<' in _sample(page, "badge:demo")
    busy = _sample(page, "button:busy")
    assert "button--busy" in busy
    assert 'aria-busy="true"' in busy
    assert re.search(r"<button[^>]* disabled", _sample(page, "button:disabled"))
    assert '<span class="stale__age">link lost</span>' in _sample(page, "stale:lost")
    loading = _sample(page, "stat:loading")
    assert 'class="stat stat--loading"' in loading
    assert 'role="status"' in loading
    assert 'class="stat stat--error"' in _sample(page, "stat:error")
    assert "Fills and closed trades appear here." in _sample(page, "table:empty")
    assert "pnl--profit" in _sample(page, "pnl:profit")
    assert "pnl--loss" in _sample(page, "pnl:loss")
    assert "pnl--zero" in _sample(page, "pnl:zero")


def test_each_journal_sample_shows_its_panel_state(logged_in):
    """covers: spec 0024 AC-1, AC-5, AC-9 (the Check panel in every state, on /styleguide)"""
    page = logged_in.get("/styleguide").text
    assert 'badge--ok">match' in _sample(page, "journal:match")
    mismatch = _sample(page, "journal:mismatch")
    assert "journal-panel--mismatch" in mismatch
    assert "badge badge--warn" in mismatch
    assert "crossed a range edge" in _sample(page, "journal:not_comparable")
    assert "belong to no position" in _sample(page, "journal:unlinked")
    for state in ("match", "mismatch", "not_comparable", "unlinked"):
        assert "journal.csv?" in _sample(page, f"journal:{state}"), state
    refused = _sample(page, "journal:refused")
    assert "No ECB rate for 2026-10-29 yet." in refused
    assert ".csv?" not in refused
    assert "Check failed." in _sample(page, "journal:error")


def test_every_type_space_radius_and_motion_token_has_a_sample(logged_in):
    """covers: AC-16"""
    from dashboard.web.styleguide import TYPE_ROLES

    from .test_design_tokens import frontmatter

    front = frontmatter(design_md().read_text(encoding="utf-8"))
    page = logged_in.get("/styleguide").text
    assert set(TYPE_ROLES) == set(front["typography"])
    missing = [f"type:{role}" for role in front["typography"] if f'data-sample="type:{role}"' not in page]
    missing += [f"space:{step}" for step in front["spacing"] if f'data-sample="space:{step}"' not in page]
    missing += [f"radius:{step}" for step in front["rounded"] if f'data-sample="radius:{step}"' not in page]
    assert missing == []
    assert 'data-sample="motion:state"' in page


def test_the_chart_sample_is_an_image_with_a_name(logged_in):
    """covers: AC-16"""
    chart = _sample(logged_in.get("/styleguide").text, "chart:default")
    assert re.search(
        r'<div class="chart__canvas" data-chart data-series=\'[^\']+\' role="img" aria-label="[^"]+">', chart
    )


def test_the_sample_candles_are_hourly_and_well_formed():
    """covers: AC-16"""
    from decimal import Decimal

    from dashboard.web.styleguide import candles

    series = candles()
    assert len(series) == 48
    times = [int(c["time"]) for c in series]
    assert all(b - a == 3600 for a, b in itertools.pairwise(times))
    for candle in series:
        prices = {k: Decimal(str(candle[k])) for k in ("open", "high", "low", "close")}
        assert all(isinstance(candle[k], str) for k in prices)
        assert prices["high"] >= max(prices["open"], prices["close"])
        assert prices["low"] <= min(prices["open"], prices["close"])


def test_the_styleguide_shows_the_number_rules_from_verify_step_18(logged_in):
    """covers: AC-16, AC-17"""
    page = logged_in.get("/styleguide").text
    assert "\N{MINUS SIGN}42.10" in page
    assert "▲</span>+312.40" in page
    assert "▼</span>\N{MINUS SIGN}87.15" in page
    assert 'title="10:30:00 CET, Amsterdam">2026-01-15 09:30:00Z</time>' in page
