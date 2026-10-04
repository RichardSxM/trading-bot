"""covers: spec 0006 AC-18 (a top bar badge whenever ``entries_blocked`` is not empty, and Overview lists the
reasons in the plain words of the Block reasons table)."""

from dashboard.live.poller import BLOCKED_EVENT, BLOCKED_REASONS_EVENT
from dashboard.live.state import NOT_BLOCKED, BlockedReason, blocked_view
from trading_core.snapshot.envelope import encode
from trading_core.strategy.blocks import PLAIN_WORDS

from .conftest import log_in, make_app

S = 1_000_000_000
NOW = 1_790_000_000 * S


def _state(**fields):
    return encode(NOW - S, {"paused": False, **fields})


def test_every_reason_reads_in_its_plain_words_in_snapshot_order():
    codes = ["exit_failed", "paused", "stale_feed"]

    view = blocked_view(_state(entries_blocked=codes))

    assert [r.code for r in view.reasons] == codes
    assert [r.words for r in view.reasons] == [PLAIN_WORDS[c] for c in codes]  # type: ignore[index]


def test_an_unknown_reason_still_shows_as_its_code():
    assert blocked_view(_state(entries_blocked=["new_reason"])).reasons == (BlockedReason("new_reason", "new_reason"),)


def test_nothing_shows_without_reasons_or_without_a_readable_snapshot():
    assert blocked_view(_state(entries_blocked=[])) == NOT_BLOCKED
    assert blocked_view(_state()) == NOT_BLOCKED  # a trader from before spec 0006
    assert blocked_view(None) == NOT_BLOCKED
    assert blocked_view(b"not a snapshot") == NOT_BLOCKED
    assert blocked_view(_state(entries_blocked="stale_feed")) == NOT_BLOCKED


def _page(client, clock, redis, **state):
    redis.values["demo:snapshot:trader_state"] = encode(clock.ns() - S, {"paused": False, **state})
    log_in(client, clock)
    return client.get("/").text


def test_the_badge_and_the_overview_list_show_while_entries_are_blocked(client, clock, redis):
    page = _page(client, clock, redis, entries_blocked=["recording_unhealthy", "stop_not_accepted"])

    assert 'sse-swap="topbar-blocked"' in page
    assert 'sse-swap="overview-blocked"' in page
    assert "blocked · 2 reasons" in page
    assert "No new entries" in page
    assert "The database is behind or not answering" in page
    assert "A stop was not accepted, so the position was closed (restart to clear)" in page


def test_nothing_blocked_shows_no_badge_and_no_list(client, clock, redis):
    page = _page(client, clock, redis, entries_blocked=[])

    assert 'class="blocked"' not in page
    assert "No new entries" not in page


def test_a_single_reason_shows_the_badge_without_a_count(client, clock, redis):
    page = _page(client, clock, redis, entries_blocked=["stale_feed"])

    assert '<span class="blocked__label">entries</span> blocked</span>' in page
    assert "No fresh prices from Kraken" in page


def test_an_unreadable_redis_renders_both_partials_empty(redis, clock):
    partials = make_app(redis, clock).state.context.poller.no_data()

    assert (partials[BLOCKED_EVENT], partials[BLOCKED_REASONS_EVENT]) == ("", "")
