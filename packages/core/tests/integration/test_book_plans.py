"""covers: spec 0033 AC-5 (``record_book_plan``: one plan row and its coin rows, idempotent on the run and the
rebalance time, a different content or an unknown instrument raises ``RecordConflictError``, the signal check)."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.book_plan_rows import BookPlanCoinRow, BookPlanReason, BookPlanRow
from trading_core.recording.enums import Env
from trading_core.recording.errors import RecordConflictError

from .conftest import INSTRUMENT, World

D = Decimal


def _plan(world: World, **overrides: object) -> BookPlanRow:
    values: dict[str, object] = {
        "env": Env.DEMO,
        "run_id": world.run_id,
        "plan_day": date(2026, 11, 13),
        "reason": BookPlanReason.DAILY,
        "rebalanced_ns": world.tick(),
        "equity": D("10000.5"),
        "n": 1,
        "gross": D("0.8"),
        "unit_vol": D("0.021"),
        "book_gross": D("1.3"),
        "book_vol_target": D("0.5"),
        "rebalance_band": D("0.3"),
        "gross_cap": D(5),
    }
    return BookPlanRow(**(values | overrides))  # type: ignore[arg-type]


def _coin(**overrides: object) -> BookPlanCoinRow:
    values: dict[str, object] = {
        "instrument_id": INSTRUMENT,
        "eligible": True,
        "signal": 1,
        "vol": D("0.03"),
        "weight": D("1.3"),
        "target_notional": D("13000.65"),
        "close_date": date(2026, 11, 13),
    }
    return BookPlanCoinRow(**(values | overrides))  # type: ignore[arg-type]


def _counts(session: Session) -> tuple[int, int]:
    plans = session.execute(select(func.count()).select_from(models.BookPlan)).scalar_one()
    coins = session.execute(select(func.count()).select_from(models.BookPlanCoin)).scalar_one()
    return plans, coins


def test_a_plan_and_its_coins_are_written_once(session: Session, world: World):
    plan = _plan(world)
    before = _counts(session)

    repository.record_book_plan(session, plan, [_coin()])
    repository.record_book_plan(session, plan, [_coin()])
    # The same content under a fresh row id (a retried write of an equal snapshot) is still a no op.
    repository.record_book_plan(session, replace(plan, id=world.run_id), [_coin()])
    session.flush()

    assert _counts(session) == (before[0] + 1, before[1] + 1)
    stored = session.execute(select(models.BookPlan).where(models.BookPlan.id == plan.id)).scalar_one()
    assert (stored.reason, stored.book_gross, stored.rebalanced_at) == ("daily", D("1.3"), plan.rebalanced_at)


def test_a_book_without_vol_sizing_stores_null_vol_fields(session: Session, world: World):
    plan = _plan(world, unit_vol=None, book_gross=None, book_vol_target=None, rebalance_band=None)

    repository.record_book_plan(session, plan, [])
    session.flush()

    stored = session.execute(select(models.BookPlan).where(models.BookPlan.id == plan.id)).scalar_one()
    assert (stored.unit_vol, stored.book_gross, stored.book_vol_target, stored.rebalance_band) == (None,) * 4


def test_a_different_equity_at_the_same_key_is_a_conflict(session: Session, world: World):
    plan = _plan(world)
    repository.record_book_plan(session, plan, [_coin()])
    session.flush()

    with pytest.raises(RecordConflictError, match="equity"):
        repository.record_book_plan(session, replace(plan, equity=D(9999)), [_coin()])


def test_a_different_coin_at_the_same_key_is_a_conflict(session: Session, world: World):
    plan = _plan(world)
    repository.record_book_plan(session, plan, [_coin()])
    session.flush()

    with pytest.raises(RecordConflictError, match="weight"):
        repository.record_book_plan(session, plan, [_coin(weight=D(2))])


def test_a_coin_with_no_instrument_row_is_a_conflict(session: Session, world: World):
    with pytest.raises(RecordConflictError, match="instruments"):
        repository.record_book_plan(session, _plan(world), [_coin(instrument_id="PF_NOPEUSD.KRAKEN")])


def test_an_ineligible_coin_must_have_signal_zero(session: Session, world: World):
    with pytest.raises(IntegrityError, match="signal_valid"):
        repository.record_book_plan(session, _plan(world), [_coin(eligible=False, signal=1)])
