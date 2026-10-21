"""covers: spec 0010 AC-8, AC-13 (the loss seed: realized today and in the total, today's opened
keys, the newest losing close, today's daily halt event, by (position_ref, opened_ns), paper only).

Other tests commit rows to the same database, so every row here sits in 2200, later than anything
else, and the window starts there.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import Env, PositionSide, PositionStatus, RiskEventKind, RunMode, Severity
from trading_core.recording.queries import loss_seed
from trading_core.recording.rows import PositionSnapshotRow, RiskEventRow

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


def test_today_s_trade_cap_event_is_seen_and_yesterday_s_is_not(session: Session, world: World):
    """covers: spec 0010 AC-11 (a same day restart knows the day's trade_cap event is stored)"""

    def trade_cap(day: datetime) -> None:
        repository.record_risk_event(
            session,
            RiskEventRow(
                env=Env.DEMO,
                event_key=f"trade_cap:{day.date().isoformat()}",
                kind=RiskEventKind.LIMIT_HIT,
                severity=Severity.WARNING,
                message="trade cap reached",
                details={"trades_today": 50, "max_trades_per_day": 50},
                run_id=world.run_id,
                occurred_ns=_ns(day + timedelta(hours=20)),
            ),
        )

    trade_cap(DAY - timedelta(days=1))
    assert not _seed(session).trade_cap_noted_today

    trade_cap(DAY)
    assert _seed(session).trade_cap_noted_today
    assert not _seed(session, env=Env.LIVE).trade_cap_noted_today


def test_a_position_closed_as_abandoned_at_a_restart_counts_with_its_marked_loss(session: Session, world: World):
    """covers: spec 0010 AC-8 (positions closed as abandoned at a demo restart count, with the marked loss)"""
    opened = DAY + timedelta(hours=1)
    row = world.position(opened_ns=_ns(opened), last_event_ns=_ns(opened))
    repository.upsert_position(session, row)
    position = session.execute(
        select(models.Position).where(models.Position.position_ref == row.position_ref)
    ).scalar_one()
    repository.record_position_snapshot(
        session,
        PositionSnapshotRow(
            env=Env.DEMO,
            position_id=position.id,
            quantity=row.quantity,
            mark_px=Decimal("59000"),
            unrealized_pnl=Decimal("-3.25"),
            currency="USD",
            captured_ns=_ns(opened + timedelta(minutes=5)),
        ),
    )
    trader_id = session.execute(
        select(models.StrategyRun.trader_id).where(models.StrategyRun.id == world.run_id)
    ).scalar_one()
    restarted = repository.start_run(
        session,
        env=Env.DEMO,
        mode=RunMode.PAPER,
        version_id=world.version_id,
        trader_id=trader_id,
        run_config={},
        started_ns=_ns(opened + timedelta(hours=1)),
    )

    closed = repository.close_abandoned(session, Env.DEMO, trader_id, restarted, _ns(opened + timedelta(hours=1)))
    seed = _seed(session)

    assert closed == [position.id]
    assert seed.realized_today == Decimal("-3.25")
    assert (row.position_ref, row.opened_ns) in seed.closed_keys
    assert seed.last_loss_closed_at == opened + timedelta(hours=1)


def test_other_modes_and_envs_are_left_out(session: Session, world: World):
    _closed(session, world, DAY + timedelta(hours=1), DAY + timedelta(hours=2), "-4")

    assert _seed(session, mode=RunMode.BACKTEST).realized_today == Decimal(0)
    assert _seed(session, env=Env.LIVE).realized_today == Decimal(0)
    assert _seed(session).realized_today == Decimal(-4)
