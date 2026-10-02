"""covers: spec 0004 AC-9 and the live poller (bounded queues, change only publishing, crash restart)."""

import asyncio
import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dashboard.live.poller import AGE_EVENT, QUEUE_SIZE, Poller, supervise
from dashboard.live.state import BAD_SNAPSHOT, LINK_LOST, NO_HEARTBEAT, AgeView, age_state, heartbeat_view
from dashboard.web.page_routes import event_stream, sse_message
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


def _poller(source, now=None):
    clock = [now if now is not None else NOW]
    poller = Poller(source, lambda view: f"{view.level}:{view.text}", 0.001, 3, 10, clock_ns=lambda: clock[0])
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
    assert events == [(AGE_EVENT, "ok:0s"), (AGE_EVENT, "ok:1s")]
    assert cached == {AGE_EVENT: "ok:1s"}


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
    assert texts[0].endswith(f":{3}s")  # the first three updates were dropped
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
    assert first == {AGE_EVENT: f"{LINK_LOST.level}:{LINK_LOST.text}"}
    assert second == {AGE_EVENT: "ok:0s"}


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
