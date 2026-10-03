"""covers: spec 0004 AC-20, Overview's Activity feed rendered with the page (empty, filled, Redis down)."""

import re
from datetime import UTC, datetime, timedelta

from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.control.messages import format_utc, stop_command, to_fields

# The count before a collapsed run: a hidden multiplication sign (U+00D7), then the number.
COUNT = r'<span aria-hidden="true">\u00d7</span>(\d+)'


def _feed(page):
    match = re.search(r'<div class="activity-feed" sse-swap="overview-activity">(.*?)</div>\s*</section>', page, re.S)
    assert match, "no Activity feed on Overview"
    return match.group(1)


def test_with_no_commands_activity_shows_its_empty_state(logged_in):
    assert "Stop commands appear here as they happen. Fills and alerts join this feed later." in _feed(
        logged_in.get("/").text
    )


def test_a_command_shows_newest_first_with_its_time_and_outcome(logged_in, redis, clock):
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    old = stop_command(now - timedelta(hours=2), 300)
    redis.append("demo:control", to_fields(old))
    redis.append(
        "demo:control:results",
        {"id": str(old.id), "outcome": "executed", "outcome_at": format_utc(old.issued_at), "schema_version": "1"},
    )
    redis.append("demo:control", to_fields(stop_command(now, 300)))
    feed = _feed(logged_in.get("/").text)
    assert feed.index("awaiting trader") < feed.index("confirmed")  # newest first, and the old one never hides
    assert format_utc(old.issued_at).replace("T", " ")[:19] in feed
    assert "command--ok" in feed
    assert "command--pending" in feed


def test_an_entry_that_does_not_parse_is_an_unknown_entry_row(logged_in, redis):
    redis.append("demo:control", {"id": "nope"})
    assert "unknown entry" in _feed(logged_in.get("/").text)


def test_redis_down_shows_activity_unavailable(logged_in, redis):
    redis.fail = RedisConnectionError("down")
    feed = _feed(logged_in.get("/").text)
    assert "Activity unavailable: no data from Redis" in feed
    assert 'role="alert"' in feed


# --- gaps found against spec 0004 (/test pass)


def test_activity_shows_the_newest_20_commands_newest_first(logged_in, redis, clock):
    """covers: AC-20"""
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    commands = [stop_command(now - timedelta(minutes=22 - i), 300) for i in range(22)]
    for command in commands:
        redis.append("demo:control", to_fields(command))
    feed = _feed(logged_in.get("/").text)
    # The newest 20 span 1 to 20 minutes back: 5 still awaiting (TTL 300 s), 15 past expiry. Each run
    # is one line with a count, its newest time, and a "from" line with its oldest time.
    assert len(re.findall(r'<li class="activity__row">', feed)) == 2
    assert re.findall(COUNT, feed) == ["5", "15"]
    times = [t[:19] for t in re.findall(r'<time[^>]*datetime="([^"]+)"', feed)]
    expected = [commands[21], commands[17], commands[16], commands[2]]
    assert times == [format_utc(c.issued_at)[:19] for c in expected]


def test_activity_colors_each_outcome_like_the_top_bar(logged_in, redis, clock):
    """covers: AC-20"""
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    failed = stop_command(now - timedelta(minutes=1), 300)
    expired = stop_command(now - timedelta(hours=1), 300)  # no result, past expires_at
    redis.append("demo:control", to_fields(expired))
    redis.append("demo:control", to_fields(failed))
    redis.append(
        "demo:control:results",
        {"id": str(failed.id), "outcome": "failed", "outcome_at": format_utc(now), "schema_version": "1"},
    )
    redis.append("demo:control", {"id": "nope"})
    feed = _feed(logged_in.get("/").text)
    assert '<span class="activity__outcome command--bad">failed</span>' in feed
    assert '<span class="activity__outcome command--bad">not handled, expired</span>' in feed
    assert '<span class="activity__cmd command--warn">unknown entry</span>' in feed


def test_activity_rows_show_their_time_through_the_ts_filter(logged_in, redis, clock):
    """covers: AC-20, AC-17 (UTC with Z, an Amsterdam tooltip)"""
    command = stop_command(datetime(2026, 7, 2, 14, 3, 7, tzinfo=UTC), 300)
    redis.append("demo:control", to_fields(command))
    feed = _feed(logged_in.get("/").text)
    assert "2026-07-02 14:03:07Z" in feed
    assert 'title="16:03:07 CEST, Amsterdam"' in feed


# --- critique round 2026-10-03: collapsed runs


def test_only_consecutive_rows_that_read_the_same_collapse(logged_in, redis, clock):
    """covers: AC-20 (a different outcome between two runs keeps them apart, newest first)"""
    now = datetime.fromtimestamp(clock.now, tz=UTC)
    commands = [stop_command(now - timedelta(minutes=m), 300) for m in (40, 39, 38, 2, 1)]
    for command in commands:
        redis.append("demo:control", to_fields(command))
    redis.append(
        "demo:control:results",
        {"id": str(commands[2].id), "outcome": "executed", "outcome_at": format_utc(now), "schema_version": "1"},
    )
    feed = _feed(logged_in.get("/").text)
    outcomes = re.findall(r'<span class="activity__outcome command--\w+">([^<]+)</span>', feed)
    assert outcomes == ["awaiting trader", "confirmed", "not handled, expired"]
    counts = re.findall(COUNT, feed)
    assert counts == ["2", "2"]
