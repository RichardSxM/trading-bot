"""covers: spec 0032 AC-9 (the failed coins alert for a book on the main trader, read from main's
``trader_state.book``, with service ``trader`` and slot ``main``) and AC-2b (one ``portfolio_close_missing`` per
plan date naming the coins kept for a missing daily close)."""

import asyncio

from trading_core.alerts.model import Tier
from trading_core.control.slots import TraderSlot
from trading_core.snapshot.envelope import encode

from .test_slots import PLAN, _ns, make, settle

MAIN_STATE = "demo:snapshot:trader_state"
MAIN_ONLY = (TraderSlot.MAIN,)


def _book(redis, clock, book=True):
    redis.values[MAIN_STATE] = encode(_ns(clock), {"paused": False, "book": book})


def _plan(redis, clock, last, failed=(), day="2026-11-10", missing=()):
    redis.values[PLAN] = encode(
        _ns(clock),
        {"last_rebalance_at": last, "failed": list(failed), "plan_day": day, "close_missing": list(missing)},
    )


def _run(steps, slots=MAIN_ONLY):
    async def run():
        alerter, clock, redis, deliverer, writer = make(slots=slots)
        assert await writer.load_once()
        observe = alerter._engine.observe
        seen = []

        def spy(obs):
            seen.append(obs)
            return observe(obs)

        alerter._engine.observe = spy
        for step in steps:
            step(redis, clock)
            await alerter.check_portfolio_failed()
            clock.advance(15)
        await settle(alerter)
        return deliverer, seen

    return asyncio.run(run())


def test_a_book_on_main_alerts_failed_coins_without_the_portfolio_slot():
    deliverer, seen = _run(
        [
            lambda r, c: (_book(r, c), _plan(r, c, 100, ["PF_ETHUSD.KRAKEN"])),
            lambda r, c: _plan(r, c, 100, ["PF_ETHUSD.KRAKEN"]),  # the same rebalance again
        ]
    )

    [push] = deliverer.sent
    assert push.tier is Tier.WARNING
    assert push.episode.fingerprint == "portfolio_coins_failed:100"
    [obs] = seen
    assert (obs.service, obs.fields["slot"]) == ("trader", "main")
    assert push.episode.details["slot"] == "main"


def test_no_book_and_no_portfolio_slot_reads_nothing():
    deliverer, _ = _run([lambda r, c: (_book(r, c, book=False), _plan(r, c, 100, ["PF_ETHUSD.KRAKEN"]))])

    assert deliverer.sent == []


def test_a_missing_book_field_counts_as_false():
    def step(redis, clock):
        redis.values[MAIN_STATE] = encode(_ns(clock), {"paused": False})
        _plan(redis, clock, 100, ["PF_ETHUSD.KRAKEN"])

    deliverer, _ = _run([step])

    assert deliverer.sent == []


def test_a_missing_close_alerts_once_per_plan_date_naming_the_coins():
    deliverer, _ = _run(
        [
            lambda r, c: (_book(r, c), _plan(r, c, 100, missing=["PF_SOLUSD.KRAKEN"])),  # the start rebalance
            lambda r, c: _plan(r, c, 200, missing=["PF_SOLUSD.KRAKEN"]),  # the daily one, same plan date
            lambda r, c: _plan(r, c, 300, day="2026-11-11"),  # next day, every close in
            lambda r, c: _plan(r, c, 400, day="2026-11-12", missing=["PF_SOLUSD.KRAKEN", "PF_ZECUSD.KRAKEN"]),
        ]
    )

    first, second = deliverer.sent
    assert first.episode.fingerprint == "portfolio_close_missing:2026-11-10"
    assert "1 coin without a daily close" in first.title
    assert first.message.startswith("PF_SOLUSD.KRAKEN kept their position")
    assert "2026-11-10" in first.message
    assert second.episode.fingerprint == "portfolio_close_missing:2026-11-12"
    assert "2 coins without a daily close" in second.title


def test_the_portfolio_slot_still_alerts_as_trader_portfolio():
    deliverer, seen = _run(
        [lambda r, c: _plan(r, c, 100, ["PF_ETHUSD.KRAKEN"])],
        slots=(TraderSlot.MAIN, TraderSlot.PORTFOLIO),
    )

    assert len(deliverer.sent) == 1
    [obs] = seen
    assert obs.service == "trader-portfolio"
    assert "slot" not in obs.fields
