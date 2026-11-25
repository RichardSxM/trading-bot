"""covers: spec 0034 AC-6 (``latest_decisions_by_coin``: the newest decision with a reason per coin, for one
trader and env across its runs)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import Env, Intent, OrderSide
from trading_core.recording.queries import latest_decisions_by_coin

from .conftest import INSTRUMENT, World


def _trader(session: Session, world: World) -> str:
    return session.execute(
        select(models.StrategyRun.trader_id).where(models.StrategyRun.id == world.run_id)
    ).scalar_one()


def test_the_newest_stop_per_coin_wins_and_other_reasons_are_ignored(session: Session, world: World):
    older = world.decision(reason="stop_vol", intent=Intent.EXIT, side=OrderSide.SELL)
    newer = world.decision(reason="stop_vol", intent=Intent.EXIT, side=OrderSide.BUY)
    other = world.decision(reason="daily", side=OrderSide.SELL)
    for row in (older, newer, other):
        repository.record_decision(session, row)

    found = latest_decisions_by_coin(session, Env.DEMO, _trader(session, world), "stop_vol")

    assert found == {INSTRUMENT: (newer.decided_ns, OrderSide.BUY)}


def test_another_trader_or_reason_finds_nothing(session: Session, world: World):
    repository.record_decision(session, world.decision(reason="stop_vol", side=OrderSide.SELL))

    assert latest_decisions_by_coin(session, Env.DEMO, "TRADER-nobody", "stop_vol") == {}
    assert latest_decisions_by_coin(session, Env.DEMO, _trader(session, world), "start") == {}
