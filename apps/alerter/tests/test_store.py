"""covers: spec 0013 AC-15 (the bounded memory queue, oldest info dropped first, nothing lost while Postgres
is down) and AC-9 (a stale open row from before an outage is closed, not doubled)."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from sqlalchemy.exc import DataError

from alerter import store as store_module
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import CheckResult, Delivery, Tier
from trading_core.recording.errors import AlertConflictError

from .conftest import NOW, Clock, FakeStore


def _episode(fingerprint, severity=Tier.WARNING, **overrides):
    engine = AlertEngine(EngineConfig(env="demo"))
    base = engine.check(CheckResult("trader_down", ok=False, at=NOW)).store[0]
    return replace(base, fingerprint=fingerprint, severity=severity, **overrides)


def test_a_newer_state_replaces_the_queued_one():
    writer = StoreWriter(FakeStore(), AlertEngine(EngineConfig(env="demo")), Clock())
    first = _episode("a")

    writer.put(first)
    writer.put(replace(first, sends=1, delivery=Delivery.SENT))

    assert writer.pending == 1
    asyncio.run(writer.flush_one())
    assert writer.pending == 0


def test_a_full_queue_drops_the_oldest_info_first(monkeypatch):
    monkeypatch.setattr(store_module, "QUEUE_MAX", 3)
    store = FakeStore()
    writer = StoreWriter(store, AlertEngine(EngineConfig(env="demo")), Clock())

    for episode in (_episode("w1"), _episode("i1", Tier.INFO), _episode("c1", Tier.CRITICAL), _episode("w2")):
        writer.put(episode)

    async def drain():
        while writer.pending:
            await writer.flush_one()

    asyncio.run(drain())
    assert sorted(e.fingerprint for e in store.saves) == ["c1", "w1", "w2"]


def test_a_down_postgres_keeps_the_episode_queued():
    store = FakeStore()
    store.down = True
    writer = StoreWriter(store, AlertEngine(EngineConfig(env="demo")), Clock())
    writer.put(_episode("a"))

    assert not asyncio.run(writer.flush_one())
    assert writer.pending == 1


def test_a_stale_open_row_is_closed_when_the_same_fingerprint_reopened_in_memory():
    old = _episode(
        "trader_down", Tier.CRITICAL, opened_at=NOW - timedelta(hours=1), last_seen_at=NOW - timedelta(hours=1)
    )
    store = FakeStore([old])
    engine = AlertEngine(EngineConfig(env="demo"))
    clock = Clock()
    writer = StoreWriter(store, engine, clock)
    engine.check(CheckResult("trader_down", ok=False, at=NOW))  # opened while Postgres was away

    assert asyncio.run(writer.load_once())

    [current] = engine.open_episodes
    assert current.opened_at == NOW

    async def drain():
        while writer.pending:
            await writer.flush_one()

    asyncio.run(drain())
    assert store.rows[("trader_down", old.opened_at)].resolved_at == NOW


async def _drain(writer):
    while writer.pending:
        await writer.flush_one()


class BadRowStore(FakeStore):
    """Refuses one fingerprint with a data error, as Postgres does for a NUL in a text field."""

    def save(self, episode):
        if episode.fingerprint == "bad":
            raise DataError("insert", {}, Exception("invalid byte sequence"))
        return super().save(episode)


def test_a_bad_row_is_dropped_and_never_blocks_the_rows_behind_it():
    store = BadRowStore()
    writer = StoreWriter(store, AlertEngine(EngineConfig(env="demo")), Clock())
    writer.put(_episode("bad"))
    writer.put(_episode("good"))

    assert asyncio.run(writer.flush_one())
    asyncio.run(_drain(writer))

    assert [e.fingerprint for e in store.saves] == ["good"]


class OneOpenStore(FakeStore):
    """Refuses a second open episode for a fingerprint, like ``uq_alerts_open``."""

    def save(self, episode):
        if episode.is_open and any(
            e.is_open and e.fingerprint == episode.fingerprint and e.opened_at != episode.opened_at
            for e in self.rows.values()
        ):
            raise AlertConflictError("another open episode")
        return super().save(episode)


def test_a_new_episode_waits_behind_its_older_resolved_one():
    old = _episode("fp", opened_at=NOW - timedelta(hours=1), last_seen_at=NOW - timedelta(hours=1))
    store = OneOpenStore([old])
    writer = StoreWriter(store, AlertEngine(EngineConfig(env="demo")), Clock())
    new = _episode("fp", opened_at=NOW, last_seen_at=NOW)
    writer.put(replace(old, resolved_at=NOW))
    writer.put(new)
    writer.put(replace(old, resolved_at=NOW, occurrences=2))  # a later state moves the old one behind

    asyncio.run(_drain(writer))

    assert store.rows[("fp", old.opened_at)].resolved_at == NOW
    assert store.rows[("fp", NOW)] == new
