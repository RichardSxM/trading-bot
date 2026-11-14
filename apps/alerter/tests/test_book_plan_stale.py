"""covers: spec 0033 AC-7 (``book_plan_stale``: main's book run, read from ``trader_state`` ``book`` and ``run_id``,
alerts once per run and UTC day when its newest ``book_plans`` row is older than 24 h 20 min, or when the run is
older than 1 h 20 min and has none; a non book trader, a fresh row, or a young run stays quiet)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from alerter.service import Alerter
from alerter.store import StoreWriter
from trading_core.alerts.checks import book_plan_stale
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Tier
from trading_core.recording.queries import BookPlanAges
from trading_core.snapshot.envelope import encode

from .conftest import Clock, FakeDeliverer, FakeRedis, FakeStore
from .test_slots import _ns, settle

MAIN_STATE = "demo:snapshot:trader_state"
RUN = uuid4()


class _Plans:
    def __init__(self, started, newest):
        self.started, self.newest = started, newest
        self.calls = 0

    def ages(self, env, run_id):
        self.calls += 1
        assert run_id == RUN
        return BookPlanAges(self.started, self.newest)


def _make(plans):
    clock = Clock()
    engine = AlertEngine(EngineConfig(env="demo"))
    writer = StoreWriter(FakeStore(), engine, clock, retry_s=0)
    redis, deliverer = FakeRedis(), FakeDeliverer()
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=writer,
        redis=redis,
        trader_down_s=45,
        clock=clock,
        book_plans=plans,
    )
    return alerter, clock, redis, deliverer, writer


def _state(redis, clock, book=True, run_id=RUN):
    redis.values[MAIN_STATE] = encode(_ns(clock), {"book": book, "run_id": str(run_id)})


def _run(make_plans, steps, book=True):
    async def run():
        clock_for_plans = Clock()
        plans = make_plans(clock_for_plans())
        alerter, clock, redis, deliverer, writer = _make(plans)
        assert await writer.load_once()
        for advance_s in steps:
            clock.advance(advance_s)
            _state(redis, clock, book=book)
            await alerter.check_book_plans()
        await settle(alerter)
        return deliverer, plans

    return asyncio.run(run())


def test_an_old_row_alerts_once_a_day():
    # The newest row is 25 h old at the first check; checks every minute, then the next UTC day.
    deliverer, _ = _run(
        lambda now: _Plans(now - timedelta(days=3), now - timedelta(hours=25)),
        [0, 60, 60, 24 * 3600],
    )

    assert len(deliverer.sent) == 2
    first = deliverer.sent[0]
    assert first.tier is Tier.WARNING
    assert first.episode.rule == "book_plan_stale"
    assert first.episode.fingerprint.startswith(f"book_plan_stale:{RUN}:")
    assert "25 h 0 min old" in first.episode.message


def test_a_new_run_without_a_row_alerts_after_its_start_window():
    deliverer, _ = _run(lambda now: _Plans(now, None), [3600, 20 * 60 + 1])

    [push] = deliverer.sent
    assert "has no book_plans row yet" in push.episode.message


def test_a_fresh_row_or_a_young_run_stays_quiet():
    fresh, _ = _run(lambda now: _Plans(now - timedelta(days=2), now - timedelta(hours=23)), [0, 3600])
    young, _ = _run(lambda now: _Plans(now, None), [3600])

    assert (fresh.sent, young.sent) == ([], [])


def test_a_trader_that_is_not_a_book_is_not_read():
    deliverer, plans = _run(lambda now: _Plans(now - timedelta(days=3), now - timedelta(days=2)), [0], book=False)

    assert deliverer.sent == []
    assert plans.calls == 0


def test_the_thresholds():
    now = Clock()()
    edge = timedelta(hours=24, minutes=20)

    assert book_plan_stale(now - timedelta(days=9), now - edge, now) is None
    assert book_plan_stale(now - timedelta(days=9), now - edge - timedelta(seconds=1), now) == "old_row"
    assert book_plan_stale(now - timedelta(hours=1, minutes=20), None, now) is None
    assert book_plan_stale(now - timedelta(hours=1, minutes=21), None, now) == "no_row"
    assert book_plan_stale(None, None, now) is None
