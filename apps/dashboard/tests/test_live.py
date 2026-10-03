"""covers: spec 0004 AC-9, AC-12, and the live poller (bounded queues, change only publishing, crash restart)."""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard.live.poller import ACTIVITY_EVENT, AGE_EVENT, COMMAND_EVENT, QUEUE_SIZE, Poller, supervise
from dashboard.live.state import (
    BAD_SNAPSHOT,
    COMMAND_LINK_LOST,
    LINK_LOST,
    NO_COMMAND,
    NO_HEARTBEAT,
    UNKNOWN_ENTRY,
    UNKNOWN_RESULT,
    AgeView,
    CommandView,
    Entry,
    activity_row,
    age_state,
    command_state,
    find_result,
    heartbeat_view,
)
from dashboard.web.page_routes import event_stream, sse_message
from trading_core.control.messages import format_utc, stop_command, to_fields
from trading_core.snapshot.envelope import encode

S = 1_000_000_000
NOW = 1_790_000_000 * S


@pytest.mark.parametrize(
    ("age_ns", "expected"),
    [
        (0, AgeView("0s", "ok")),
        (3 * S, AgeView("3s", "ok")),
        (3 * S + 1, AgeView("3s", "warn")),
        (10 * S, AgeView("10s", "warn")),
        (10 * S + 1, AgeView("10s", "bad")),
        (29 * S, AgeView("29s", "bad")),
        (-5 * S, AgeView("0s", "ok", skewed=True)),
    ],
)
def test_age_state_thresholds(age_ns, expected):
    assert age_state(NOW, NOW - age_ns, amber_s=3, red_s=10) == expected


def test_heartbeat_view_names_missing_and_broken_values():
    assert heartbeat_view(None, NOW, 3, 10) is NO_HEARTBEAT
    assert heartbeat_view("nope", NOW, 3, 10) is BAD_SNAPSHOT
    assert heartbeat_view(encode(NOW - 2 * S, {}), NOW, 3, 10) == AgeView("2s", "ok")


class Source:
    def __init__(self, value=None):
        self.value = value
        self.error = None
        self.reads = 0

    async def __call__(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.value


class Streams:
    """The command and result streams as newest first XREVRANGE replies; counts result reads."""

    def __init__(self):
        self.commands = []
        self.results = []
        self.result_reads = 0
        self.error = None

    async def read_command(self):
        if self.error is not None:
            raise self.error
        return self.commands[:20]

    async def read_results(self):
        self.result_reads += 1
        return self.results[:200]


def _render(view):
    return f"{view.level}:{view.text}"


def _render_rows(rows):
    return "lost" if rows is None else "|".join(f"{row.cmd}:{row.text}" for row in rows)


def _poller(source, now=None, streams=None, read_timeout_s=1.0):
    clock = [now if now is not None else NOW]
    streams = streams if streams is not None else Streams()
    poller = Poller(
        source,
        _render,
        0.001,
        3,
        10,
        read_command=streams.read_command,
        read_results=streams.read_results,
        render_command=_render,
        render_activity=_render_rows,
        hide_after_s=900,
        read_timeout_s=read_timeout_s,
        clock_ns=lambda: clock[0],
    )
    return poller, clock


def test_tick_publishes_only_when_the_html_changes():
    async def run():
        source = Source(encode(NOW, {}))
        poller, clock = _poller(source)
        queue = poller.subscribe()
        await poller.tick()
        await poller.tick()
        clock[0] += S
        await poller.tick()
        return [queue.get_nowait() for _ in range(queue.qsize())], poller.cached

    events, cached = asyncio.run(run())
    assert events == [(AGE_EVENT, "ok:0s"), (COMMAND_EVENT, "none:none"), (ACTIVITY_EVENT, ""), (AGE_EVENT, "ok:1s")]
    assert cached == {AGE_EVENT: "ok:1s", COMMAND_EVENT: "none:none", ACTIVITY_EVENT: ""}


def test_a_slow_subscriber_loses_the_oldest_updates_not_the_newest():
    async def run():
        poller, clock = _poller(Source(encode(NOW, {})))
        queue = poller.subscribe()
        for _ in range(QUEUE_SIZE + 3):
            await poller.tick()
            clock[0] += S
        return [queue.get_nowait()[1] for _ in range(queue.qsize())]

    texts = asyncio.run(run())
    assert len(texts) == QUEUE_SIZE
    assert texts[0].endswith(f":{3}s")  # the first five updates (three ages, the command, Activity) were dropped
    assert texts[-1].endswith(f":{QUEUE_SIZE + 2}s")


def test_a_redis_error_renders_link_lost_and_the_next_tick_recovers():
    async def run():
        source = Source(encode(NOW, {}))
        source.error = RedisConnectionError("down")
        poller, _ = _poller(source)
        first = await poller.render_now()
        source.error = None
        return first, await poller.render_now()

    first, second = asyncio.run(run())
    assert first == {AGE_EVENT: _render(LINK_LOST), COMMAND_EVENT: _render(COMMAND_LINK_LOST), ACTIVITY_EVENT: "lost"}
    assert second == {AGE_EVENT: "ok:0s", COMMAND_EVENT: "none:none", ACTIVITY_EVENT: ""}


def test_a_redis_that_does_not_answer_renders_link_lost_within_the_read_deadline():
    async def never():  # like redis-py retrying a hung Redis far past the deadline
        await asyncio.Event().wait()

    async def run():
        poller, _ = _poller(never, read_timeout_s=0.05)
        start = time.monotonic()
        partials = await poller.render_now()
        return partials, time.monotonic() - start

    partials, elapsed = asyncio.run(run())
    assert partials == {
        AGE_EVENT: _render(LINK_LOST),
        COMMAND_EVENT: _render(COMMAND_LINK_LOST),
        ACTIVITY_EVENT: "lost",
    }
    assert elapsed < 0.5


def test_clock_skew_logs_one_warning_per_minute(caplog):
    async def run():
        poller, _ = _poller(Source(encode(NOW + 5 * S, {})))
        for _ in range(3):
            await poller.render_now()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert [r.fields["code"] for r in caplog.records] == ["clock_skew"]


def test_the_poller_idles_without_subscribers_and_wakes_on_subscribe():
    async def run():
        source = Source(encode(NOW, {}))
        poller, _ = _poller(source)
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.02)
        idle_reads = source.reads
        queue = poller.subscribe()
        await asyncio.sleep(0.02)
        poller.unsubscribe(queue)
        task.cancel()
        return idle_reads, source.reads

    idle_reads, reads = asyncio.run(run())
    assert idle_reads == 0
    assert reads > 0


def test_a_crashed_poller_logs_error_and_restarts(caplog):
    class Crashing:
        def __init__(self):
            self.runs = 0

        async def run(self):
            self.runs += 1
            raise RuntimeError("bug")

    async def run():
        crashing = Crashing()
        task = asyncio.create_task(supervise(crashing, restart_after_s=0.001))
        await asyncio.sleep(0.05)
        task.cancel()
        return crashing.runs

    with caplog.at_level(logging.ERROR):
        runs = asyncio.run(run())
    assert runs > 1
    assert caplog.records[0].fields == {"code": "poller_crashed"}
    assert caplog.records[0].levelno == logging.ERROR


def test_sse_message_prefixes_every_line():
    assert sse_message("topbar-age", "a\nb") == "event: topbar-age\ndata: a\ndata: b\n\n"
    assert sse_message("ping", "") == "event: ping\ndata: \n\n"


def test_event_stream_sends_the_current_partials_then_changes_and_pings_and_ends_with_the_session():
    async def run():
        poller, _ = _poller(Source(encode(NOW, {})))
        now = [1000.0]
        stream = event_stream(poller, {AGE_EVENT: "ok:0s"}, ends_at=1000.5, clock=lambda: now[0], ping_every_s=0.01)
        out = [await anext(stream)]
        poller._publish((AGE_EVENT, "ok:1s"))
        out.append(await anext(stream))
        out.append(await anext(stream))  # nothing new: a ping
        now[0] = 1001.0  # the session would have expired
        out.extend([message async for message in stream])
        return out, poller

    out, poller = asyncio.run(run())
    assert out[:3] == [
        sse_message(AGE_EVENT, "ok:0s"),
        sse_message(AGE_EVENT, "ok:1s"),
        sse_message("ping", ""),
    ]
    assert poller._subscribers == set()  # unsubscribed when the stream ended


# Command state (AC-12)

T0 = datetime(2026, 10, 2, 14, 0, 0, tzinfo=UTC)
COMMAND = stop_command(T0, 300)
COMMAND_FIELDS = to_fields(COMMAND)


def _result(outcome, at=T0 + timedelta(seconds=2), command_id=None):
    return {
        "id": command_id or str(COMMAND.id),
        "outcome": outcome,
        "outcome_at": format_utc(at),
        "schema_version": "1",
    }


@pytest.mark.parametrize(
    ("outcome", "text", "level", "final"),
    [
        ("received", "stop sent · received by trader", "pending", False),
        ("executed", "stop sent · confirmed", "ok", True),
        ("duplicate", "stop sent · duplicate", "none", True),
        ("failed", "stop sent · failed", "bad", True),
        ("expired", "stop sent · expired", "bad", True),
    ],
)
def test_each_outcome_has_its_label(outcome, text, level, final):
    view = command_state(COMMAND_FIELDS, _result(outcome), T0 + timedelta(seconds=3), 900)
    assert view == CommandView(text, level, final=final)


def test_no_command_and_no_result_yet():
    assert command_state(None, None, T0, 900) is NO_COMMAND
    assert command_state(COMMAND_FIELDS, None, T0 + timedelta(seconds=300), 900) == CommandView(
        "stop sent · awaiting trader", "pending"
    )


def test_no_result_after_expiry_is_not_handled_and_a_late_result_still_counts():
    after = T0 + timedelta(seconds=301)
    view = command_state(COMMAND_FIELDS, None, after, 900)
    assert view == CommandView("stop sent · not handled, expired", "bad")
    assert not view.final  # the poller keeps reading results
    late = command_state(COMMAND_FIELDS, _result("executed", at=after), after, 900)
    assert late.text == "stop sent · confirmed"


def test_a_final_state_hides_fifteen_minutes_after_it_was_reached():
    at = T0 + timedelta(seconds=2)
    shown = command_state(COMMAND_FIELDS, _result("executed", at=at), at + timedelta(seconds=900), 900)
    hidden = command_state(COMMAND_FIELDS, _result("executed", at=at), at + timedelta(seconds=901), 900)
    assert shown.text == "stop sent · confirmed"
    assert (hidden.text, hidden.level, hidden.final) == ("none", "none", True)
    expired = command_state(COMMAND_FIELDS, None, COMMAND.expires_at + timedelta(seconds=901), 900)
    assert (expired.text, expired.level) == ("none", "none")


def test_received_is_never_hidden():
    view = command_state(COMMAND_FIELDS, _result("received"), T0 + timedelta(hours=2), 900)
    assert view.text == "stop sent · received by trader"


@pytest.mark.parametrize(
    ("command", "result"),
    [
        ({"id": "nope"}, None),
        (COMMAND_FIELDS, _result("exploded")),
        (COMMAND_FIELDS, {**_result("executed"), "schema_version": "9"}),
    ],
)
def test_an_entry_that_does_not_parse_is_unknown_result(command, result):
    assert command_state(command, result, T0, 900) is UNKNOWN_RESULT
    assert UNKNOWN_RESULT.level == "warn"


def test_find_result_takes_the_newest_match():
    other = str(uuid.uuid4())
    entries: list[Entry] = [
        (b"4-0", {b"id": other.encode(), b"outcome": b"failed"}),
        (b"3-0", {b"id": str(COMMAND.id).encode(), b"outcome": b"executed"}),
        (b"2-0", {b"id": str(COMMAND.id).encode(), b"outcome": b"received"}),
    ]
    assert find_result(entries, str(COMMAND.id)) == entries[1][1]
    assert find_result(entries, str(uuid.uuid4())) is None


def _clock_for(moment):
    return int(moment.timestamp()) * S


def test_the_poller_reads_results_only_until_the_outcome_is_final():
    async def run():
        streams = Streams()
        streams.commands = [(b"1-0", COMMAND_FIELDS)]
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0 + timedelta(seconds=5)), streams=streams)
        first = await poller.command_view()
        streams.results = [(b"2-0", _result("received"))]
        second = await poller.command_view()
        streams.results = [(b"3-0", _result("executed")), *streams.results]
        third = await poller.command_view()
        reads_at_final = streams.result_reads
        fourth = await poller.command_view()
        return first.text, second.text, third.text, fourth.text, reads_at_final, streams.result_reads

    first, second, third, fourth, reads_at_final, reads = asyncio.run(run())
    assert (first, second, third, fourth) == (
        "stop sent · awaiting trader",
        "stop sent · received by trader",
        "stop sent · confirmed",
        "stop sent · confirmed",
    )
    assert reads == reads_at_final == 3


def test_a_command_read_error_renders_both_partials_link_lost():
    async def run():
        streams = Streams()
        streams.error = RedisConnectionError("down")
        poller, _ = _poller(Source(encode(NOW, {})), streams=streams)
        return await poller.render_now()

    assert asyncio.run(run()) == {
        AGE_EVENT: _render(LINK_LOST),
        COMMAND_EVENT: _render(COMMAND_LINK_LOST),
        ACTIVITY_EVENT: "lost",
    }


def test_an_invalid_entry_logs_one_warning_not_one_per_tick(caplog):
    async def run():
        streams = Streams()
        streams.commands = [(b"1-0", COMMAND_FIELDS)]
        streams.results = [(b"2-0", _result("exploded"))]
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0), streams=streams)
        views = [await poller.command_view() for _ in range(3)]
        streams.results = [(b"3-0", {**_result("executed"), "schema_version": "9"}), *streams.results]
        views.append(await poller.command_view())
        return views

    with caplog.at_level(logging.WARNING):
        views = asyncio.run(run())
    assert all(view is UNKNOWN_RESULT for view in views)
    assert [r.fields["code"] for r in caplog.records] == ["control_result_invalid"] * 2


def test_note_sent_publishes_awaiting_trader_at_once():
    async def run():
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0))
        queue = poller.subscribe()
        poller.note_sent(COMMAND)
        return queue.get_nowait(), poller.cached

    event, cached = asyncio.run(run())
    assert event == (COMMAND_EVENT, "pending:stop sent · awaiting trader")
    assert cached[COMMAND_EVENT] == "pending:stop sent · awaiting trader"


# Activity feed (AC-20)


@pytest.mark.parametrize(
    ("result", "at", "text", "level"),
    [
        (None, T0 + timedelta(seconds=5), "awaiting trader", "pending"),
        (None, T0 + timedelta(seconds=301), "not handled, expired", "bad"),
        (_result("received"), T0 + timedelta(seconds=5), "received by trader", "pending"),
        (_result("executed"), T0 + timedelta(hours=3), "confirmed", "ok"),
        (_result("failed"), T0 + timedelta(hours=3), "failed", "bad"),
        (_result("duplicate"), T0 + timedelta(hours=3), "duplicate", "none"),
        (_result("expired"), T0 + timedelta(hours=3), "expired", "bad"),
    ],
)
def test_an_activity_row_uses_the_top_bar_words_and_never_hides(result, at, text, level):
    row = activity_row(COMMAND_FIELDS, result, at)
    assert (row.cmd, row.issued_at, row.text, row.level) == ("stop", COMMAND.issued_at, text, level)
    # The top bar hid the same final outcome long ago.
    if row.final:
        assert command_state(COMMAND_FIELDS, result, at, 900).text == NO_COMMAND.text


def test_an_entry_that_does_not_parse_is_one_unknown_entry_row():
    assert activity_row({"id": "nope"}, None, T0) is UNKNOWN_ENTRY
    assert UNKNOWN_ENTRY.level == "warn"
    row = activity_row(COMMAND_FIELDS, _result("exploded"), T0)
    assert (row.cmd, row.text, row.level, row.invalid) == ("stop", "unknown result", "warn", True)


def _commands(count):
    """``count`` stop commands one minute apart, as a newest first XREVRANGE reply."""
    msgs = [stop_command(T0 + timedelta(minutes=i), 300) for i in range(count)]
    entries = [(f"{i + 1}-0".encode(), to_fields(msg)) for i, msg in enumerate(msgs)]
    return list(reversed(entries)), msgs


def test_the_poller_lists_the_newest_20_commands_newest_first():
    async def run():
        streams = Streams()
        streams.commands, _ = _commands(22)
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0 + timedelta(minutes=30)), streams=streams)
        return (await poller.render_now())[ACTIVITY_EVENT]

    html = asyncio.run(run())
    rows = html.split("|")
    assert len(rows) == 20
    assert rows == ["stop:not handled, expired"] * 20


def test_activity_reads_results_until_every_command_in_view_is_final():
    async def run():
        streams = Streams()
        streams.commands, msgs = _commands(2)
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0 + timedelta(minutes=2)), streams=streams)
        streams.results = [
            (b"9-0", _result("executed", at=T0 + timedelta(minutes=1, seconds=2), command_id=str(msgs[1].id))),
            (b"8-0", _result("received", at=T0 + timedelta(seconds=2), command_id=str(msgs[0].id))),
        ]
        first = (await poller.render_now())[ACTIVITY_EVENT]
        streams.results.insert(0, (b"10-0", _result("failed", command_id=str(msgs[0].id))))
        second = (await poller.render_now())[ACTIVITY_EVENT]
        reads_at_final = streams.result_reads
        third = (await poller.render_now())[ACTIVITY_EVENT]
        return first, second, third, reads_at_final, streams.result_reads

    first, second, third, reads_at_final, reads = asyncio.run(run())
    assert first == "stop:confirmed|stop:received by trader"
    assert second == third == "stop:confirmed|stop:failed"
    assert reads == reads_at_final == 2


def test_an_invalid_activity_entry_warns_once_not_once_per_tick(caplog):
    async def run():
        streams = Streams()
        commands, _ = _commands(1)
        streams.commands = [(b"5-0", {"id": "nope"}), *commands]
        poller, _ = _poller(Source(encode(NOW, {})), now=_clock_for(T0), streams=streams)
        return [(await poller.render_now())[ACTIVITY_EVENT] for _ in range(3)]

    with caplog.at_level(logging.WARNING):
        views = asyncio.run(run())
    assert views == ["unknown entry:|stop:awaiting trader"] * 3
    assert [r.fields["code"] for r in caplog.records] == ["control_result_invalid"]


# --- gaps found against spec 0004 (/test pass)


def test_clock_skew_warns_again_once_a_minute_has_passed(caplog):
    """covers: AC-9 (one clock_skew WARNING per 60 s, not one ever)"""
    mono = [1000.0]
    streams = Streams()
    poller = Poller(
        Source(encode(NOW + 5 * S, {})),
        _render,
        0.001,
        3,
        10,
        read_command=streams.read_command,
        read_results=streams.read_results,
        render_command=_render,
        render_activity=_render_rows,
        hide_after_s=900,
        read_timeout_s=1.0,
        clock_ns=lambda: NOW,
        monotonic=lambda: mono[0],
    )

    async def run():
        await poller.render_now()
        mono[0] += 59.9
        await poller.render_now()
        mono[0] += 0.1
        await poller.render_now()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert [r.fields["code"] for r in caplog.records] == ["clock_skew", "clock_skew"]


def test_not_handled_expired_still_shows_at_exactly_fifteen_minutes_after_expiry():
    """covers: AC-12 (hidden after, not at, the 15 minute mark)"""
    view = command_state(COMMAND_FIELDS, None, COMMAND.expires_at + timedelta(seconds=900), 900)
    assert view == CommandView("stop sent · not handled, expired", "bad")


def test_two_different_bad_entries_warn_once_each(caplog):
    """covers: AC-20 (once per entry, not once per poll)"""
    streams = Streams()
    streams.commands = [(b"2-0", {b"id": b"nope"}), (b"1-0", {b"cmd": b"stop"})]

    async def run():
        poller, _ = _poller(Source(encode(NOW, {})), streams=streams)
        for _ in range(3):
            await poller.tick()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    codes = [r.fields["code"] for r in caplog.records if r.levelno == logging.WARNING]
    assert codes == ["control_result_invalid", "control_result_invalid"]


def test_a_stop_reaches_activity_on_the_next_poll():
    """covers: AC-20 (refreshed over SSE within one poll of a new command)"""
    streams = Streams()

    async def run():
        poller, _ = _poller(Source(encode(NOW, {})), streams=streams)
        queue = poller.subscribe()
        await poller.tick()
        while not queue.empty():
            queue.get_nowait()
        msg = stop_command(datetime.fromtimestamp(NOW / S, tz=UTC), 300)
        streams.commands.insert(0, (b"1-0", {k.encode(): v.encode() for k, v in to_fields(msg).items()}))
        poller.note_sent(msg)
        await poller.tick()
        return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(run())
    activity = [html for name, html in events if name == ACTIVITY_EVENT]
    assert activity == ["stop:awaiting trader"]


@pytest.mark.parametrize(("newer", "text"), [(199, "stop sent · confirmed"), (200, "stop sent · awaiting trader")])
def test_only_the_newest_200_results_are_searched(logged_in, redis, clock, newer, text):
    """covers: AC-12 (a matching result behind 200 newer ones is not found)"""
    command = stop_command(datetime.fromtimestamp(clock.now, tz=UTC), 300)
    redis.append("demo:control", to_fields(command))
    at = format_utc(datetime.fromtimestamp(clock.now, tz=UTC))

    def result(command_id):
        return {"id": command_id, "outcome": "executed", "outcome_at": at, "schema_version": "1"}

    redis.append("demo:control:results", result(str(command.id)))
    for _ in range(newer):
        redis.append("demo:control:results", result(str(uuid.uuid4())))
    assert text in logged_in.get("/").text
