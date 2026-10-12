"""covers: spec 0010 AC-8, AC-13 (the loss seed: realized today and in the total, today's opened
keys, the newest losing close, today's daily halt event, by (position_ref, opened_ns), paper only).

Other tests commit rows to the same database, so every row here sits in 2200, later than anything
else, and the window starts there.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import Env, PositionSide, PositionStatus, RiskEventKind, RunMode, Severity
from trading_core.recording.queries import loss_seed
from trading_core.recording.rows import RiskEventRow

from .conftest import World

TOTAL_SINCE = datetime(2200, 1, 1, tzinfo=UTC)
DAY = datetime(2200, 1, 3, tzinfo=UTC)


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _closed(session: Session, world: World, opened: datetime, closed: datetime, pnl: str, ref: str | None = None):
    row = world.position(
        side=PositionSide.FLAT,
        status=PositionStatus.CLOSED,
        quantity=Decimal(0),
        realized_pnl=Decimal(pnl),
        opened_ns=_ns(opened),
        closed_at=closed,
        last_event_ns=_ns(closed),
        **({"position_ref": ref} if ref else {}),
    )
    repository.upsert_position(session, row)
    return (row.position_ref, row.opened_ns)


def _seed(session: Session, env: Env = Env.DEMO, mode: RunMode = RunMode.PAPER):
    return loss_seed(session, env, mode, DAY, TOTAL_SINCE)


def test_realized_today_and_total_and_the_keys(session: Session, world: World):
    before = _closed(session, world, TOTAL_SINCE - timedelta(days=2), TOTAL_SINCE - timedelta(days=1), "-500")
    earlier = _closed(session, world, DAY - timedelta(days=1), DAY - timedelta(hours=20), "-7")
    # Three round trips on one NETTING position id: told apart by opened_ns.
    ref = f"P-{uuid7().hex[-12:]}"
    trips = [
        _closed(session, world, DAY + timedelta(hours=h), DAY + timedelta(hours=h, minutes=10), pnl, ref)
        for h, pnl in ((1, "-2"), (2, "3.5"), (3, "-1"))
    ]
    open_row = world.position(opened_ns=_ns(DAY + timedelta(hours=4)))
    repository.upsert_position(session, open_row)

    seed = _seed(session)

    assert seed.realized_today == Decimal("0.5")
    assert seed.realized_total == Decimal("-6.5")
    assert seed.closed_keys == frozenset([earlier, *trips])
    assert before not in seed.closed_keys
    assert seed.opened_today_keys == frozenset([*trips, (open_row.position_ref, open_row.opened_ns)])
    assert seed.last_loss_closed_at == DAY + timedelta(hours=3, minutes=10)
    assert not seed.daily_halt_today


def test_a_daily_halt_event_today_is_seen_and_other_reasons_are_not(session: Session, world: World):
    def halt(reason: str, at: datetime) -> None:
        repository.record_risk_event(
            session,
            RiskEventRow(
                env=Env.DEMO,
                event_key=f"halt:{uuid7()}",
                kind=RiskEventKind.HALT,
                severity=Severity.CRITICAL,
                message="halted",
                details={"reason": reason, "source": "trader"},
                run_id=world.run_id,
                occurred_ns=_ns(at),
            ),
        )

    halt("manual_stop", DAY + timedelta(hours=1))
    halt("daily_loss_limit", DAY - timedelta(minutes=1))  # yesterday
    assert not _seed(session).daily_halt_today

    halt("daily_loss_limit", DAY + timedelta(hours=2))
    assert _seed(session).daily_halt_today


def test_other_modes_and_envs_are_left_out(session: Session, world: World):
    _closed(session, world, DAY + timedelta(hours=1), DAY + timedelta(hours=2), "-4")

    assert _seed(session, mode=RunMode.BACKTEST).realized_today == Decimal(0)
    assert _seed(session, env=Env.LIVE).realized_today == Decimal(0)
    assert _seed(session).realized_today == Decimal(-4)
