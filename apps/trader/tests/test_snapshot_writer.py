"""covers: spec 0004 AC-8, the heartbeat writer keeps the trader running through Redis trouble."""

import asyncio
import itertools
import json
import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from trader.snapshot_writer import log_writer_crash, write_heartbeats


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    async def set(self, name, value, *, ex):
        if self.fail:
            raise RedisConnectionError("redis down")
        self.writes.append((name, value, ex))


def _run(coro, seconds):
    async def run():
        task = asyncio.create_task(coro)
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_writes_an_envelope_with_a_30_second_expiry_every_interval():
    redis = FakeRedis()
    _run(write_heartbeats(redis, "demo:snapshot:heartbeat", lambda: {"process": "trader"}, 0.01, lambda: 42), 0.05)

    assert len(redis.writes) >= 3
    key, value, ex = redis.writes[0]
    assert (key, ex) == ("demo:snapshot:heartbeat", 30)
    assert json.loads(value) == {"schema_version": 1, "ts_ns": 42, "data": {"process": "trader"}}


def test_a_state_not_known_yet_writes_nothing_that_round():
    redis = FakeRedis()
    states = itertools.chain([None, None], itertools.repeat({"paused": True}))
    _run(write_heartbeats(redis, "demo:snapshot:trader_state", lambda: next(states), 0.01, lambda: 42), 0.05)

    assert redis.writes
    assert all(json.loads(value)["data"] == {"paused": True} for _, value, _ in redis.writes)


def test_a_redis_outage_logs_at_most_one_warning_a_minute_and_keeps_trying(caplog):
    redis = FakeRedis(fail=True)
    now = [0.0]

    def monotonic():
        now[0] += 10.0  # each failed write is 10 s later
        return now[0]

    with caplog.at_level(logging.WARNING):
        _run(write_heartbeats(redis, "k", dict, 0.001, monotonic=monotonic), 0.1)

    warnings = [r for r in caplog.records if r.fields["code"] == "snapshot_write_failed"]
    attempts = now[0] / 10.0
    assert attempts > 12
    # One warning per 6 attempts (60 s), the first at once.
    assert len(warnings) == pytest.approx(attempts // 6 + 1, abs=1)
    assert warnings[0].levelno == logging.WARNING


def test_a_crashed_writer_logs_critical(caplog):
    async def crash():
        raise RuntimeError("bug")

    async def run():
        task = asyncio.create_task(crash())
        task.add_done_callback(log_writer_crash)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with caplog.at_level(logging.CRITICAL):
        asyncio.run(run())

    (record,) = caplog.records
    assert record.levelno == logging.CRITICAL
    assert record.fields == {"code": "snapshot_writer_crashed"}


def test_a_cancelled_writer_logs_nothing(caplog):
    async def run():
        task = asyncio.create_task(asyncio.sleep(10))
        task.add_done_callback(log_writer_crash)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert caplog.records == []


# --- gaps found against spec 0004 (/test pass)


def test_the_default_interval_is_one_second():
    """covers: AC-8 (every 1 s)"""
    import inspect

    from trader.snapshot_writer import HEARTBEAT_TTL_S, WRITE_INTERVAL_S

    assert WRITE_INTERVAL_S == 1.0
    assert inspect.signature(write_heartbeats).parameters["interval_s"].default == WRITE_INTERVAL_S
    assert HEARTBEAT_TTL_S == 30


def test_no_two_outage_warnings_are_less_than_a_minute_apart():
    """covers: AC-8 (at most one WARNING snapshot_write_failed per 60 s)"""
    redis = FakeRedis(fail=True)
    now = [0.0]
    stamps = []

    def monotonic():
        now[0] += 7.0
        return now[0]

    class Stamp(logging.Handler):
        def emit(self, record):
            stamps.append(now[0])

    handler = Stamp(level=logging.WARNING)
    logging.getLogger("trader.snapshot_writer").addHandler(handler)
    try:
        _run(write_heartbeats(redis, "k", dict, 0.001, monotonic=monotonic), 0.1)
    finally:
        logging.getLogger("trader.snapshot_writer").removeHandler(handler)

    assert len(stamps) >= 2
    assert all(b - a >= 60 for a, b in itertools.pairwise(stamps))
    assert all(b - a < 67 for a, b in itertools.pairwise(stamps))  # and not fewer than needed


def test_an_os_error_is_an_outage_too(caplog):
    """covers: AC-8 (a refused socket keeps the writer going like a Redis error)"""

    class Refused(FakeRedis):
        async def set(self, name, value, *, ex):
            self.writes.append(name)
            raise ConnectionRefusedError

    redis = Refused()
    with caplog.at_level(logging.WARNING):
        _run(write_heartbeats(redis, "k", dict, 0.001), 0.05)
    assert len(redis.writes) > 2  # kept trying
    assert [r.fields["code"] for r in caplog.records] == ["snapshot_write_failed"]
    assert caplog.records[0].fields["error"] == "ConnectionRefusedError"


def test_a_writer_that_ends_normally_logs_nothing(caplog):
    """covers: AC-8"""

    async def run():
        task = asyncio.create_task(asyncio.sleep(0))
        task.add_done_callback(log_writer_crash)
        await task
        await asyncio.sleep(0)

    asyncio.run(run())
    assert caplog.records == []
