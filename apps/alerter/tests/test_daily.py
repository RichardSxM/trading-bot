"""covers: spec 0013 AC-18 (the daily note at 08:00 Amsterdam, its catch up on start, one note per day
across restarts through its ``daily`` row, never muted, waiting for Postgres) and the backup health it
reports."""

import asyncio
from datetime import UTC, datetime, timedelta

from alerter.service import Alerter
from alerter.sources.containers import backup_health
from alerter.sources.docker_api import Container
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Hold, HoldKind, PushReason, Tier

from .conftest import Clock, FakeDeliverer, FakeRedis, FakeStore

# 2026-10-16 is summer time: 08:00 in Amsterdam is 06:00 UTC.
BEFORE = datetime(2026, 10, 16, 5, 59, tzinfo=UTC)
EIGHT = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


class MarkerCheckingDeliverer(FakeDeliverer):
    """Records, for each push, whether its row was already stored when it went out."""

    def __init__(self, store: FakeStore) -> None:
        super().__init__(Delivery.DRY_RUN)
        self.store = store
        self.stored_first: list[bool] = []

    async def send(self, push):
        self.stored_first.append((push.episode.fingerprint, push.episode.opened_at) in self.store.rows)
        return await super().send(push)


def make(tmp_path, store, at):
    clock = Clock(at)
    engine = AlertEngine(EngineConfig(env="demo"))
    writer = StoreWriter(store, engine, clock, retry_s=0)
    deliverer = MarkerCheckingDeliverer(store)
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=writer,
        redis=FakeRedis(),
        trader_down_s=45,
        clock=clock,
        liveness_file=tmp_path / "alive",
    )
    return alerter, clock, deliverer, writer, engine


async def flush(writer):
    while writer.pending:
        await writer.flush_one()


def test_nothing_before_eight_then_one_silent_note_stored_before_it_is_pushed(tmp_path):
    async def run():
        store = FakeStore()
        alerter, clock, deliverer, writer, _ = make(tmp_path, store, BEFORE)
        await alerter.daily.step()
        assert deliverer.sent == []

        clock.at = EIGHT
        await alerter.daily.step()
        clock.advance(60)
        await alerter.daily.step()
        await flush(writer)

        assert [(p.reason, p.tier) for p in deliverer.sent] == [(PushReason.DAILY, Tier.INFO)]
        assert deliverer.stored_first == [True]
        assert deliverer.sent[0].episode.title == "[demo] Daily note 2026-10-16"
        assert "Backup: unknown." in deliverer.sent[0].message
        (row,) = [e for e in store.rows.values() if e.rule == "daily"]
        assert (row.fingerprint, row.delivery, row.sends, row.is_open) == (
            "daily:2026-10-16",
            Delivery.DRY_RUN,
            1,
            False,
        )

    asyncio.run(run())


def test_a_restart_the_same_day_sends_nothing(tmp_path):
    async def run():
        store = FakeStore()
        first, _, sent_first, writer, _ = make(tmp_path, store, EIGHT)
        await first.daily.step()
        await flush(writer)

        again, _, sent_again, _, _ = make(tmp_path, store, EIGHT + timedelta(hours=2))
        await again.daily.step()

        assert len(sent_first.sent) == 1
        assert sent_again.sent == []

    asyncio.run(run())


def test_a_start_after_eight_with_no_row_catches_up_once(tmp_path):
    async def run():
        store = FakeStore()
        alerter, clock, deliverer, _, _ = make(tmp_path, store, EIGHT + timedelta(hours=7))
        await alerter.daily.step()
        clock.advance(60)
        await alerter.daily.step()

        assert [p.episode.fingerprint for p in deliverer.sent] == ["daily:2026-10-16"]

    asyncio.run(run())


def test_the_note_goes_out_during_a_mute(tmp_path):
    async def run():
        alerter, _, deliverer, _, engine = make(tmp_path, FakeStore(), EIGHT)
        engine.open_hold(Hold(HoldKind.MUTE, "m", EIGHT, EIGHT + timedelta(hours=12)))

        await alerter.daily.step()

        assert [p.reason for p in deliverer.sent] == [PushReason.DAILY]

    asyncio.run(run())


def test_it_waits_for_postgres_then_sends_once_the_same_day(tmp_path):
    async def run():
        store = FakeStore()
        store.down = True
        alerter, clock, deliverer, _, _ = make(tmp_path, store, EIGHT)
        await alerter.daily.step()
        clock.advance(60)
        await alerter.daily.step()
        assert deliverer.sent == []

        store.down = False
        clock.advance(60)
        await alerter.daily.step()

        assert [p.episode.fingerprint for p in deliverer.sent] == ["daily:2026-10-16"]

    asyncio.run(run())


def test_a_day_postgres_was_down_all_day_gets_no_late_note(tmp_path):
    async def run():
        store = FakeStore()
        store.down = True
        alerter, clock, deliverer, _, _ = make(tmp_path, store, EIGHT)
        await alerter.daily.step()

        store.down = False
        clock.at = EIGHT + timedelta(hours=23)  # 07:00 the next local day: the 16th is over, the 17th not due
        await alerter.daily.step()
        assert deliverer.sent == []

        clock.at = EIGHT + timedelta(days=1)
        await alerter.daily.step()
        assert [p.episode.fingerprint for p in deliverer.sent] == ["daily:2026-10-17"]

    asyncio.run(run())


def test_backup_health_in_plain_words():
    def listing(status):
        return [
            Container("a", "trader", "running", "Up 2 hours (healthy)"),
            Container("b", "backup", "running", status),
        ]

    assert backup_health(listing("Up 3 days (healthy)")) == "healthy"
    assert backup_health(listing("Up 3 days (unhealthy)")) == "unhealthy"
    assert backup_health(listing("Up 5 seconds (health: starting)")) == "starting"
    assert backup_health(listing("Exited (1) 2 hours ago")) == "Exited (1) 2 hours ago"
    assert backup_health([Container("a", "trader", "running", "Up")]) == "no backup container"
