"""covers: spec 0023 AC-19 (the Reconciliation page list: reported checks newest first, 50 per page, each with
its findings and actions in the order they were written).

Other tests may commit checks to the same database, so every row here starts in 2095 and later, newer than
anything else, and the assertions look at the head of the list.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import (
    ActionResult,
    Env,
    MismatchAction,
    MismatchKind,
    ReconcileOutcome,
    ReconcileTrigger,
)
from trading_core.recording.queries import reconciliations_page
from trading_core.recording.rows import ReconciliationMismatchRow, ReconciliationRow

from .conftest import INSTRUMENT, World

BASE = datetime(2095, 1, 1, tzinfo=UTC)


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _check(session: Session, world: World, at: datetime, **fields: Any) -> ReconciliationRow:
    row = ReconciliationRow(
        env=Env.DEMO,
        run_id=world.run_id,
        trigger=ReconcileTrigger.START,
        started_ns=_ns(at),
        finished_ns=_ns(at) + 1_000_000,
        outcome=ReconcileOutcome.CLEAN,
    )
    row = dataclasses.replace(row, **fields)
    repository.record_reconciliation(session, row)
    return row


def test_checks_come_newest_first_with_their_findings_in_order(session: Session, world: World):
    older = _check(session, world, BASE, exchange_orders=0, exchange_positions=0)
    newer = _check(
        session,
        world,
        BASE + timedelta(minutes=5),
        outcome=ReconcileOutcome.MISMATCH,
        exchange_orders=1,
        exchange_positions=1,
        mismatch_count=2,
    )
    for kind, action in (
        (MismatchKind.UNKNOWN_ORDER, MismatchAction.CANCELLED),
        (MismatchKind.UNKNOWN_POSITION, MismatchAction.FLATTENED),
    ):
        repository.record_reconciliation_mismatch(
            session,
            ReconciliationMismatchRow(
                env=Env.DEMO,
                reconciliation_id=newer.id,
                kind=kind,
                instrument_id=INSTRUMENT,
                exchange_side="long",
                exchange_qty=Decimal("0.0001"),
                action=action,
                action_result=ActionResult.PENDING,
            ),
        )
    session.flush()

    first, second = reconciliations_page(session, Env.DEMO, 1)[:2]

    assert (first.id, second.id) == (newer.id, older.id)
    assert (first.outcome, first.exchange_orders, first.exchange_positions) == ("mismatch", 1, 1)
    assert [(m.kind, m.action, m.action_result) for m in first.mismatches] == [
        ("unknown_order", "cancelled", "pending"),
        ("unknown_position", "flattened", "pending"),
    ]
    assert (first.mismatches[0].exchange_side, first.mismatches[0].exchange_qty) == ("long", Decimal("0.0001"))
    assert second.mismatches == ()


def test_a_failed_check_keeps_its_error_and_null_counts(session: Session, world: World):
    _check(session, world, BASE + timedelta(days=1), outcome=ReconcileOutcome.FAILED, error="exchange_unavailable")
    session.flush()

    (head,) = reconciliations_page(session, Env.DEMO, 1)[:1]

    assert (head.outcome, head.error, head.exchange_orders, head.exchange_positions) == (
        "failed",
        "exchange_unavailable",
        None,
        None,
    )


def test_pages_hold_fifty_and_a_page_past_the_end_is_empty(session: Session, world: World):
    for minute in range(51):
        _check(session, world, BASE + timedelta(days=2, minutes=minute))
    session.flush()

    assert len(reconciliations_page(session, Env.DEMO, 1)) == 50
    assert reconciliations_page(session, Env.DEMO, 10_000) == []
    with pytest.raises(ValueError, match="at least 1"):
        reconciliations_page(session, Env.DEMO, 0)
