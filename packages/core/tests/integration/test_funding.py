"""covers: spec 0011 AC-14 (``record_funding`` bumps ``funding_total`` once; the position upsert leaves it
alone), AC-16 (the loss seed counts stored payments, once by key), AC-17 (the run's funding sum).

Rows sit in 2200 (like ``test_loss_seed``) so no other test's rows fall in the seed's window.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import Env, FundingSource, RunMode
from trading_core.recording.errors import MissingParentError
from trading_core.recording.queries import loss_seed, run_funding_total
from trading_core.recording.rows import FundingRow

from .conftest import INSTRUMENT, World

TOTAL_SINCE = datetime(2200, 1, 1, tzinfo=UTC)
DAY = datetime(2200, 1, 3, tzinfo=UTC)
HOUR_NS = 3_600 * 1_000_000_000


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _payment(world: World, position_id, paid: datetime, amount: str) -> FundingRow:
    return FundingRow(
        env=Env.DEMO,
        run_id=world.run_id,
        position_id=position_id,
        instrument_id=INSTRUMENT,
        amount=Decimal(amount),
        currency="USD",
        rate=Decimal("0.0001"),
        source=FundingSource.COMPUTED,
        paid_ns=_ns(paid),
    )


def _open(session: Session, world: World, opened: datetime):
    row = world.position(opened_ns=_ns(opened), last_event_ns=_ns(opened))
    repository.upsert_position(session, row)
    return row


def _funding_total(session: Session, position_id) -> Decimal:
    position = session.get(models.Position, position_id)
    assert position is not None
    session.refresh(position)
    return position.funding_total


def test_a_new_payment_bumps_funding_total_and_a_repeat_does_not(session: Session, world: World):
    position = _open(session, world, DAY)
    payment = _payment(world, position.id, DAY + timedelta(hours=1), "-0.006")

    assert repository.record_funding(session, payment) is True
    assert repository.record_funding(session, payment) is False  # the spool replays the same row

    assert _funding_total(session, position.id) == Decimal("-0.006")


def test_funding_total_is_the_sum_of_its_payments(session: Session, world: World):
    position = _open(session, world, DAY)
    for hours, amount in ((1, "-0.006"), (2, "0.002"), (3, "-0.001")):
        repository.record_funding(session, _payment(world, position.id, DAY + timedelta(hours=hours), amount))

    assert _funding_total(session, position.id) == Decimal("-0.005")
    assert run_funding_total(session, world.run_id) == Decimal("-0.005")


def test_a_later_position_upsert_leaves_funding_total_alone(session: Session, world: World):
    position = _open(session, world, DAY)
    repository.record_funding(session, _payment(world, position.id, DAY + timedelta(hours=1), "-0.006"))

    # The mapper always sends funding_total = 0; a newer update must not overwrite the booked sum.
    later = replace(position, quantity=Decimal("0.002"), last_event_ns=position.last_event_ns + HOUR_NS * 2)
    repository.upsert_position(session, later)

    assert _funding_total(session, position.id) == Decimal("-0.006")
    stored = session.get(models.Position, position.id)
    assert stored is not None
    assert stored.quantity == Decimal("0.002")


def test_a_payment_for_an_unknown_position_is_refused(session: Session, world: World):
    from uuid import uuid7

    with pytest.raises(MissingParentError):
        repository.record_funding(session, _payment(world, uuid7(), DAY, "-1"))


def test_a_run_with_no_payments_sums_to_zero(session: Session, world: World):
    assert run_funding_total(session, world.run_id) == Decimal(0)


def test_the_loss_seed_counts_funding_by_paid_day_once(session: Session, world: World):
    position = _open(session, world, DAY - timedelta(days=1))
    yesterday = _payment(world, position.id, DAY - timedelta(hours=1), "-2")
    today = _payment(world, position.id, DAY + timedelta(hours=1), "-3")
    for row in (yesterday, today):
        repository.record_funding(session, row)

    seed = loss_seed(session, Env.DEMO, RunMode.PAPER, DAY, TOTAL_SINCE)

    assert (seed.funding_today, seed.funding_total) == (Decimal(-3), Decimal(-5))
    assert seed.funding_keys == frozenset(
        {(INSTRUMENT, yesterday.paid_ns, position.id), (INSTRUMENT, today.paid_ns, position.id)}
    )
