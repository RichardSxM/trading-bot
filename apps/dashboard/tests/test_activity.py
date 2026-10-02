"""covers: spec 0004 AC-20, Overview's Activity feed rendered with the page (empty, filled, Redis down)."""

import re
from datetime import UTC, datetime, timedelta

from redis.exceptions import ConnectionError as RedisConnectionError

from trading_core.control.messages import format_utc, stop_command, to_fields


def _feed(page):
    match = re.search(r'<div class="activity-feed" sse-swap="overview-activity">(.*?)</div>\s*</section>', page, re.S)
    assert match, "no Activity feed on Overview"
    return match.group(1)


def test_with_no_commands_activity_shows_its_empty_state(logged_in):
    assert "Fills, commands, and alerts appear here as they happen." in _feed(logged_in.get("/").text)


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
    assert "Activity unavailable: link lost" in feed
    assert 'role="alert"' in feed
