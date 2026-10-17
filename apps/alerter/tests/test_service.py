"""covers: spec 0013 AC-1 (trader down from the heartbeat, its note, nothing while Redis is down), AC-2 (no
ping while the Redis read is stale), AC-9 (a restart pushes nothing twice, even with a slow load), AC-13
(dry run), AC-14 (/fail while a push fails), AC-15 (a push never waits for Postgres)."""

import asyncio
import contextlib

from alerter.service import Alerter
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, PushReason, Tier
from trading_core.snapshot.envelope import encode

from .conftest import Clock, FakeDeliverer, FakeRedis, FakeStore

KEY = "demo:snapshot:heartbeat"


class FakeWatchdog:
    def __init__(self) -> None:
        self.pings: list[bool] = []

    async def ping(self, *, failing: bool) -> bool:
        self.pings.append(failing)
        return True


def make(tmp_path, *, store=None, delivery=Delivery.DRY_RUN, watchdog=None, clock=None):
    clock = clock or Clock()
    engine = AlertEngine(EngineConfig(env="demo"))
    store = store or FakeStore()
    writer = StoreWriter(store, engine, clock, retry_s=0)
    redis = FakeRedis()
    deliverer = FakeDeliverer(delivery)
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=writer,
        redis=redis,
        trader_down_s=45,
        watchdog=watchdog,
        clock=clock,
        liveness_file=tmp_path / "alive",
    )
    return alerter, clock, redis, deliverer, writer, store, engine


def beat(redis, clock):
    redis.values[KEY] = encode(int(clock().timestamp()) * 1_000_000_000, {"state": "running"})


async def settle(alerter):
    """Let every push task started so far finish."""
    while alerter._pushes:
        await asyncio.gather(*list(alerter._pushes))


def test_a_stopped_trader_gives_one_dry_run_push_and_a_note_when_it_returns(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, writer, store, _ = make(tmp_path)
        assert await writer.load_once()
        beat(redis, clock)
        await alerter.check_heartbeat()
        assert deliverer.sent == []

        clock.advance(46)  # the trader stopped writing 46 s ago
        await alerter.check_heartbeat()
        await settle(alerter)
        for _ in range(24):  # two more minutes of checks and ticks
            clock.advance(5)
            await alerter.check_heartbeat()
            alerter.tick()
            await settle(alerter)
        assert [(p.reason, p.tier) for p in deliverer.sent] == [(PushReason.OPEN, Tier.CRITICAL)]

        beat(redis, clock)
        await alerter.check_heartbeat()
        await settle(alerter)
        while writer.pending:
            await writer.flush_one()
        return deliverer, store

    deliverer, store = asyncio.run(run())

    assert [p.reason for p in deliverer.sent] == [PushReason.OPEN, PushReason.RESOLVED]
    assert deliverer.sent[1].tier is Tier.INFO
    [row] = [e for e in store.rows.values() if e.rule == "trader_down"]
    assert (row.delivery, row.sends, row.resolved_at is not None) == (Delivery.DRY_RUN, 1, True)


def test_a_missing_key_counts_as_down_once_the_alerter_has_watched_45_s(tmp_path):
    async def run():
        alerter, clock, _, deliverer, *_ = make(tmp_path)
        await alerter.check_heartbeat()  # a cold start: the trader may not be up yet
        clock.advance(44)
        await alerter.check_heartbeat()
        await settle(alerter)
        assert deliverer.sent == []
        clock.advance(1)
        await alerter.check_heartbeat()
        await settle(alerter)
        return deliverer

    assert asyncio.run(run()).sent[0].message == "No trader heartbeat in Redis."


def test_redis_down_raises_no_trader_down_and_stops_the_ping(tmp_path):
    async def run():
        watchdog = FakeWatchdog()
        alerter, clock, redis, deliverer, *_ = make(tmp_path, watchdog=watchdog)
        beat(redis, clock)
        await alerter.check_heartbeat()
        await alerter.ping()

        redis.fail = True
        clock.advance(61)
        await alerter.check_heartbeat()
        await alerter.ping()
        await settle(alerter)
        return watchdog, deliverer

    watchdog, deliverer = asyncio.run(run())

    assert deliverer.sent == []
    assert watchdog.pings == [False]  # the second ping was skipped: the Redis read is stale


def test_a_failed_push_turns_the_ping_into_fail_until_a_retry_succeeds(tmp_path):
    async def run():
        watchdog = FakeWatchdog()
        alerter, clock, _, deliverer, *_ = make(tmp_path, delivery=Delivery.FAILED, watchdog=watchdog)
        clock.advance(45)
        await alerter.check_heartbeat()
        await settle(alerter)
        await alerter.ping()

        deliverer.delivery = Delivery.SENT
        clock.advance(60)
        alerter.retry()
        await settle(alerter)
        await alerter.check_heartbeat()
        await alerter.ping()
        return watchdog, deliverer

    watchdog, deliverer = asyncio.run(run())

    assert watchdog.pings == [True, False]
    assert len(deliverer.sent) == 2


def test_a_push_goes_out_while_postgres_is_down_and_is_stored_when_it_returns(tmp_path):
    async def run():
        store = FakeStore()
        store.down = True
        alerter, clock, _, deliverer, writer, *_ = make(tmp_path, store=store)
        assert not await writer.load_once()

        clock.advance(45)
        await alerter.check_heartbeat()
        await settle(alerter)
        assert len(deliverer.sent) == 1
        assert store.saves == []

        store.down = False
        assert await writer.load_once()
        while writer.pending:
            assert await writer.flush_one()
        return store

    store = asyncio.run(run())

    [row] = store.rows.values()
    assert (row.rule, row.delivery, row.sends) == ("trader_down", Delivery.DRY_RUN, 1)


def test_a_restart_with_the_episode_stored_pushes_nothing_twice(tmp_path):
    async def run():
        first, clock, _, sent_first, writer, store, _ = make(tmp_path)
        await writer.load_once()
        clock.advance(45)
        await first.check_heartbeat()
        await settle(first)
        while writer.pending:
            await writer.flush_one()

        second, clock2, _, sent_second, writer2, *_ = make(tmp_path, store=store, clock=Clock(clock()))
        await writer2.load_once()
        clock2.advance(45)
        await second.check_heartbeat()
        second.tick()
        await settle(second)
        return sent_first, sent_second

    sent_first, sent_second = asyncio.run(run())

    assert len(sent_first.sent) == 1
    assert sent_second.sent == []


class SlowLoad(FakeStore):
    """Postgres answers the load only from its ``answer_from``th call on."""

    def __init__(self, stored, answer_from: int) -> None:
        super().__init__()
        self.rows = stored
        self.loads = 0
        self.answer_from = answer_from

    def load_active(self, failed_since):
        self.loads += 1
        self.down = self.loads < self.answer_from
        try:
            return super().load_active(failed_since)
        finally:
            self.down = False


def stored_trader_down(tmp_path):
    """The rows of a first alerter that pushed ``trader_down`` and stored it, and its clock."""

    async def run():
        first, clock, _, _, writer, store, _ = make(tmp_path)
        await writer.load_once()
        clock.advance(45)
        await first.check_heartbeat()
        await settle(first)
        while writer.pending:
            await writer.flush_one()
        return store.rows, clock

    return asyncio.run(run())


async def run_briefly(alerter, seconds=0.3):
    task = asyncio.create_task(alerter.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await settle(alerter)


def test_a_restart_waits_for_a_slow_load_and_pushes_nothing_twice(tmp_path):
    rows, clock = stored_trader_down(tmp_path)

    async def run():
        store = SlowLoad(rows, answer_from=3)  # two failed loads first
        second, clock2, redis, sent, *_ = make(tmp_path, store=store, clock=Clock(clock()))
        redis.values[KEY] = encode(int(clock2().timestamp() - 600) * 1_000_000_000, {"state": "running"})
        await run_briefly(second)
        return sent, store

    sent, store = asyncio.run(run())

    assert store.loads == 3
    assert sent.sent == []


def test_with_postgres_still_down_the_checks_start_after_the_wait_and_push(tmp_path):
    async def run():
        store = FakeStore()
        store.down = True
        alerter, clock, redis, sent, *_ = make(tmp_path, store=store)
        alerter._load_wait_s = 0.05
        redis.values[KEY] = encode(int(clock().timestamp() - 600) * 1_000_000_000, {"state": "running"})
        await run_briefly(alerter)
        return sent

    sent = asyncio.run(run())

    assert [p.episode.rule for p in sent.sent] == ["trader_down"]


def test_the_liveness_file_is_touched(tmp_path):
    alerter, *_ = make(tmp_path)

    alerter.touch_liveness()

    assert (tmp_path / "alive").exists()
