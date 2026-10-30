"""covers: spec 0023 AC-19 (the Kraken check badge in the top bar)."""

import re
from datetime import UTC, datetime

import pytest

from dashboard.live.state import RECONCILE_CHECKING, RECONCILE_NO_DATA, ReconcileView, reconcile_view
from trading_core.snapshot.envelope import encode

STATE_KEY = "demo:snapshot:trader_state"
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp()) * 1_000_000_000


def _raw(**data):
    return encode(NOW_NS, {"halted": False} | data)


def test_without_trader_state_the_badge_says_no_data():
    assert reconcile_view(None, NOW) is RECONCILE_NO_DATA


def test_an_unreadable_snapshot_is_bad():
    assert reconcile_view("nope", NOW) == ReconcileView("bad snapshot", "bad")


def test_before_the_first_check_the_badge_says_checking():
    assert reconcile_view(_raw(last_clean_check_ns=None, last_check_outcome=None), NOW) is RECONCILE_CHECKING
    assert reconcile_view(_raw(), NOW) is RECONCILE_CHECKING  # a trader from before spec 0023


def test_a_clean_check_shows_its_age():
    raw = _raw(last_clean_check_ns=NOW_NS - 12 * 1_000_000_000, last_check_outcome="clean")

    assert reconcile_view(raw, NOW) == ReconcileView("clean · 12s", "ok")


@pytest.mark.parametrize(("outcome", "text"), [("mismatch", "mismatch"), ("failed", "Kraken unknown")])
def test_a_bad_last_outcome_shows_in_the_alert_colour_even_after_an_earlier_clean_check(outcome, text):
    raw = _raw(last_clean_check_ns=NOW_NS - 60 * 1_000_000_000, last_check_outcome=outcome)

    assert reconcile_view(raw, NOW) == ReconcileView(text, "bad")


def test_halted_wins_over_a_clean_check():
    raw = _raw(halted=True, last_clean_check_ns=NOW_NS, last_check_outcome="clean")

    assert reconcile_view(raw, NOW) == ReconcileView("halted", "bad")


def test_a_boolean_is_not_a_time():
    assert reconcile_view(_raw(last_clean_check_ns=True), NOW) is RECONCILE_CHECKING


def test_the_top_bar_renders_the_badge(logged_in, redis, clock):
    redis.values[STATE_KEY] = encode(clock.ns(), {"paused": False, "last_check_outcome": "mismatch"})

    page = logged_in.get("/").text

    assert re.search(
        r'sse-swap="topbar-check"><a class="check check--bad" href="/reconciliations".*kraken check</span> mismatch',
        page,
    )


def test_the_top_bar_badge_says_no_data_without_trader_state(logged_in, redis):
    redis.values.pop(STATE_KEY, None)

    assert re.search(r'class="check check--lost" href="/reconciliations".*no data', logged_in.get("/").text)
