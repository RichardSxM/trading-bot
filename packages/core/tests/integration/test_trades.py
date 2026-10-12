"""covers: spec 0006 AC-19 (the Trades lists: closed paper round trips with side from the earliest fill
and exit reason, and fills, each newest first and paged by 50); spec 0010 AC-1 (a kill switch exit).

Other tests commit demo positions and fills to the same database, so every row here sits in 2090
and later, newer than anything else, and the assertions look at the head of each list.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import (
    Env,
    Intent,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RunMode,
    Severity,
)
from trading_core.recording.queries import fills_page, round_trips_page
from trading_core.recording.rows import RiskEventRow
from trading_core.strategy.placeholder import ExitReason

from .conftest import World

BASE = datetime(2090, 1, 1, tzinfo=UTC)
OPPOSITE = {OrderSide.BUY: OrderSide.SELL, OrderSide.SELL: OrderSide.BUY}


def _ns(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000


def _fill(session: Session, world: World, position_id: UUID, side: OrderSide, at: datetime, **order: object) -> None:
    placed = world.order(side=side, price=None, status=OrderStatus.FILLED, filled_qty=Decimal("0.001"), **order)
    repository.upsert_order(session, placed)
    repository.record_fill(
        session, world.fill(placed.client_order_id, side=side, position_id=position_id, filled_ns=_ns(at))
    )


def _round_trip(
    session: Session,
    world: World,
    closed_at: datetime,
    *,
    entry: OrderSide = OrderSide.BUY,
    exit_type: OrderType | None = OrderType.MARKET,
    exit_intent: Intent | None = Intent.EXIT,
    linked: bool = True,
    kill_switch: bool = False,
) -> UUID:
    """A closed position, entered by market on ``entry`` and closed by ``exit_type``.

    ``exit_intent`` is the exit order's own decision (``None``: it carries the entry's).
    ``exit_type=None`` leaves the exit fill out; ``linked=False`` leaves out every fill.
    ``kill_switch`` makes the exit a kill switch order: its origin, and no decision.
    """
    opened_at = closed_at - timedelta(minutes=10)
    position = world.position(
        side=PositionSide.FLAT,
        status=PositionStatus.CLOSED,
        quantity=Decimal(0),
        avg_exit_px=Decimal("60100"),
        realized_pnl=Decimal("0.04"),
        opened_ns=_ns(opened_at),
        closed_at=closed_at,
        last_event_ns=_ns(closed_at),
    )
    repository.upsert_position(session, position)
    if not linked:
        return position.id
    decision = world.decision(side=entry)
    repository.record_decision(session, decision)
    _fill(session, world, position.id, entry, opened_at, decision_id=decision.id, order_type=OrderType.MARKET)
    if exit_type is None:
        return position.id
    exit_decision_id = decision.id
    if exit_intent is not None:
        exit_decision = world.decision(intent=exit_intent, side=OPPOSITE[entry])
        repository.record_decision(session, exit_decision)
        exit_decision_id = exit_decision.id
    trigger = Decimal("59000") if exit_type == OrderType.STOP_MARKET else None
    _fill(
        session,
        world,
        position.id,
        OPPOSITE[entry],
        closed_at,
        decision_id=None if kill_switch else exit_decision_id,
        origin=OrderOrigin.KILL_SWITCH if kill_switch else OrderOrigin.STRATEGY,
        order_type=exit_type,
        trigger_price=trigger,
    )
    return position.id


def _abandon(session: Session, world: World, position_id: UUID) -> None:
    repository.record_risk_event(
        session,
        RiskEventRow(
            env=Env.DEMO,
            event_key=f"abandoned:{position_id}",
            kind=RiskEventKind.RECONCILE_MISMATCH,
            severity=Severity.CRITICAL,
            message="position abandoned at restart",
            run_id=world.run_id,
            occurred_ns=world.tick(),
        ),
    )


def _head(session: Session, count: int):
    return list(round_trips_page(session, Env.DEMO, 1)[:count])


def test_the_side_comes_from_the_earliest_fill(session: Session, world: World):
    long_id = _round_trip(session, world, BASE + timedelta(hours=3), entry=OrderSide.BUY)
    short_id = _round_trip(session, world, BASE + timedelta(hours=2), entry=OrderSide.SELL)
    bare_id = _round_trip(session, world, BASE + timedelta(hours=1), linked=False)

    rows = _head(session, 3)

    assert [r.position_id for r in rows] == [long_id, short_id, bare_id]
    assert [r.side for r in rows] == [PositionSide.LONG, PositionSide.SHORT, None]


def test_each_exit_reason(session: Session, world: World):
    at = BASE + timedelta(days=1)
    stop = _round_trip(session, world, at + timedelta(hours=6), exit_type=OrderType.STOP_MARKET, exit_intent=None)
    timed = _round_trip(session, world, at + timedelta(hours=5))
    failure = _round_trip(session, world, at + timedelta(hours=4), exit_intent=None)
    abandoned = _round_trip(session, world, at + timedelta(hours=3), exit_type=None)
    _abandon(session, world, abandoned)
    abandoned_bare = _round_trip(session, world, at + timedelta(hours=2), linked=False)
    _abandon(session, world, abandoned_bare)
    bare = _round_trip(session, world, at + timedelta(hours=1), linked=False)
    killed = _round_trip(session, world, at + timedelta(hours=7), exit_intent=None, kill_switch=True)

    rows = {r.position_id: r.exit_reason for r in _head(session, 7)}

    assert rows == {
        killed: ExitReason.KILL_SWITCH,
        stop: ExitReason.STOP,
        timed: ExitReason.TIME,
        failure: ExitReason.STOP_FAILURE,
        abandoned: ExitReason.ABANDONED,
        abandoned_bare: ExitReason.ABANDONED,
        bare: None,
    }


def test_a_round_trip_carries_its_position_figures(session: Session, world: World):
    closed_at = BASE + timedelta(days=2)
    position_id = _round_trip(session, world, closed_at)

    (row,) = _head(session, 1)

    assert row.position_id == position_id
    assert (row.opened_at, row.closed_at) == (closed_at - timedelta(minutes=10), closed_at)
    assert (row.size, row.avg_entry_px, row.avg_exit_px) == (Decimal("0.001"), Decimal("60000.5"), Decimal("60100"))
    assert (row.realized_pnl, row.fees, row.currency) == (Decimal("0.04"), Decimal("0.03000025"), "USD")


def test_open_positions_and_other_modes_are_left_out(session: Session, world: World):
    closed_at = BASE + timedelta(days=3)
    kept = _round_trip(session, world, closed_at)
    repository.upsert_position(session, world.position(opened_ns=_ns(closed_at + timedelta(hours=1))))
    backtest = World(
        version_id=world.version_id,
        run_id=repository.start_run(
            session,
            env=Env.DEMO,
            mode=RunMode.BACKTEST,
            version_id=world.version_id,
            trader_id=f"BACKTEST-{uuid7().hex[-8:]}",
            run_config={},
            started_ns=world.clock,
        ),
    )
    _round_trip(session, backtest, closed_at + timedelta(hours=2))

    assert [r.position_id for r in _head(session, 1)] == [kept]
    assert round_trips_page(session, Env.LIVE, 1) == []


def test_round_trips_page_by_fifty_newest_first(session: Session, world: World):
    at = BASE + timedelta(days=4)
    ids = [_round_trip(session, world, at + timedelta(minutes=i), linked=False) for i in range(51)]

    first, second = round_trips_page(session, Env.DEMO, 1), round_trips_page(session, Env.DEMO, 2)

    assert [r.position_id for r in first] == ids[:0:-1]
    assert second[0].position_id == ids[0]
    assert round_trips_page(session, Env.DEMO, 100_000) == []


def test_a_tie_on_closed_at_orders_by_id(session: Session, world: World):
    closed_at = BASE + timedelta(days=5)
    ids = [_round_trip(session, world, closed_at, linked=False) for _ in range(2)]

    assert [r.position_id for r in _head(session, 2)] == sorted(ids, reverse=True)


def test_fills_page_newest_first(session: Session, world: World):
    at = BASE + timedelta(days=6)
    _round_trip(session, world, at, entry=OrderSide.SELL)

    exit_fill, entry_fill = fills_page(session, Env.DEMO, 1)[:2]

    assert (exit_fill.filled_at, exit_fill.side) == (at, OrderSide.BUY)
    assert (entry_fill.filled_at, entry_fill.side) == (at - timedelta(minutes=10), OrderSide.SELL)
    assert (entry_fill.quantity, entry_fill.price, entry_fill.fee) == (
        Decimal("0.001"),
        Decimal("60000.5"),
        Decimal("0.03000025"),
    )
    assert fills_page(session, Env.DEMO, 100_000) == []


@pytest.mark.parametrize("page", [0, -1])
def test_a_page_below_one_is_refused(session: Session, page: int):
    with pytest.raises(ValueError, match="at least 1"):
        round_trips_page(session, Env.DEMO, page)
    with pytest.raises(ValueError, match="at least 1"):
        fills_page(session, Env.DEMO, page)
