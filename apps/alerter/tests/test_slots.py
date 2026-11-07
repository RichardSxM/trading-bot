"""covers: spec 0030 AC-17 (``ALERTER_TRADER_SLOTS``: each listed trader's heartbeat and entry blocks, with the
slot in the alert text, and one ``portfolio_coins_failed`` per rebalance that left coins in ``failed``)."""

import asyncio

import pytest
from pydantic import ValidationError

from alerter.service import Alerter
from alerter.settings import AlerterSettings
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import PushReason, Tier
from trading_core.control.slots import TraderSlot
from trading_core.snapshot.envelope import encode

from .conftest import Clock, FakeDeliverer, FakeRedis, FakeStore

MAIN_BEAT = "demo:snapshot:heartbeat"
PORTFOLIO_BEAT = "demo:snapshot:portfolio_heartbeat"
PORTFOLIO_STATE = "demo:snapshot:portfolio_trader_state"
PLAN = "demo:snapshot:portfolio_plan"
BOTH = (TraderSlot.MAIN, TraderSlot.PORTFOLIO)


def make(slots=BOTH):
    clock = Clock()
    engine = AlertEngine(EngineConfig(env="demo"))
    writer = StoreWriter(FakeStore(), engine, clock, retry_s=0)
    redis = FakeRedis()
    deliverer = FakeDeliverer()
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=writer,
        redis=redis,
        trader_down_s=45,
        slots=slots,
        clock=clock,
    )
    return alerter, clock, redis, deliverer, writer


def _ns(clock):
    return int(clock().timestamp()) * 1_000_000_000


def beat(redis, clock, key):
    redis.values[key] = encode(_ns(clock), {"state": "running"})


async def settle(alerter):
    while alerter._pushes:
        await asyncio.gather(*list(alerter._pushes))


def test_a_stale_portfolio_heartbeat_alerts_naming_the_slot_while_main_stays_quiet():
    async def run():
        alerter, clock, redis, deliverer, writer = make()
        assert await writer.load_once()
        beat(redis, clock, MAIN_BEAT)
        beat(redis, clock, PORTFOLIO_BEAT)
        await alerter.check_heartbeat()
        clock.advance(46)
        beat(redis, clock, MAIN_BEAT)  # main keeps beating, the portfolio stopped 46 s ago
        await alerter.check_heartbeat()
        await settle(alerter)
        return deliverer

    [push] = asyncio.run(run()).sent
    assert (push.reason, push.tier) == (PushReason.OPEN, Tier.CRITICAL)
    assert push.episode.fingerprint == "trader_down:portfolio"
    assert "Trader down (portfolio trader)" in push.title


def test_main_alone_ignores_the_portfolio_heartbeat_and_keeps_its_own_text():
    async def run():
        alerter, clock, redis, deliverer, writer = make(slots=(TraderSlot.MAIN,))
        assert await writer.load_once()
        beat(redis, clock, MAIN_BEAT)
        beat(redis, clock, PORTFOLIO_BEAT)
        await alerter.check_heartbeat()
        clock.advance(46)  # both stopped
        await alerter.check_heartbeat()
        await settle(alerter)
        return deliverer

    [push] = asyncio.run(run()).sent
    assert push.episode.fingerprint == "trader_down"
    assert push.title.endswith("Trader down")


def test_a_stuck_portfolio_entry_block_alerts_with_its_own_fingerprint():
    async def run():
        alerter, clock, redis, deliverer, writer = make()
        assert await writer.load_once()
        since = _ns(clock)
        for _ in range(242):  # an hour and a little more, a check every 15 s
            redis.values[PORTFOLIO_STATE] = encode(
                _ns(clock), {"entries_blocked": ["stale_feed"], "entries_blocked_since": {"stale_feed": since}}
            )
            await alerter.check_entry_blocks()
            alerter.tick()
            await settle(alerter)
            clock.advance(15)
        return deliverer

    [push] = asyncio.run(run()).sent
    assert push.episode.fingerprint == "entry_block:portfolio:stale_feed"
    assert "(portfolio trader)" in push.title
    assert "(stale_feed)" in push.message


def _plan(redis, clock, last, failed):
    redis.values[PLAN] = encode(_ns(clock), {"last_rebalance_at": last, "failed": failed})


def test_failed_coins_alert_once_per_rebalance_naming_the_coins():
    async def run():
        alerter, clock, redis, deliverer, writer = make()
        assert await writer.load_once()
        _plan(redis, clock, 100, ["PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN"])
        for _ in range(3):  # the same rebalance seen again and again
            await alerter.check_portfolio_failed()
            clock.advance(15)
        _plan(redis, clock, 200, [])  # the next rebalance went through
        await alerter.check_portfolio_failed()
        _plan(redis, clock, 300, ["PF_ETHUSD.KRAKEN"])
        await alerter.check_portfolio_failed()
        await settle(alerter)
        return deliverer

    first, second = asyncio.run(run()).sent
    assert first.tier is Tier.WARNING
    assert first.episode.fingerprint == "portfolio_coins_failed:100"
    assert "2 coins failed" in first.title
    assert first.message.startswith("PF_ETHUSD.KRAKEN, PF_SOLUSD.KRAKEN did not reach their target")
    assert second.episode.fingerprint == "portfolio_coins_failed:300"
    assert "1 coin failed" in second.title


def test_failed_coins_are_not_read_unless_the_portfolio_is_watched():
    async def run():
        alerter, clock, redis, deliverer, writer = make(slots=(TraderSlot.MAIN,))
        assert await writer.load_once()
        _plan(redis, clock, 100, ["PF_ETHUSD.KRAKEN"])
        await alerter.check_portfolio_failed()
        await settle(alerter)
        return deliverer

    assert asyncio.run(run()).sent == []


def test_the_slots_setting_defaults_to_main_and_reads_a_comma_list(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert AlerterSettings().trader_slots() == (TraderSlot.MAIN,)
    monkeypatch.setenv("ALERTER_TRADER_SLOTS", " main, portfolio,main ")
    assert AlerterSettings().trader_slots() == BOTH


@pytest.mark.parametrize("value", ["", " , ", "main,shadow"])
def test_a_bad_slots_setting_refuses(monkeypatch, value):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("ALERTER_TRADER_SLOTS", value)
    with pytest.raises(ValidationError):
        AlerterSettings()
