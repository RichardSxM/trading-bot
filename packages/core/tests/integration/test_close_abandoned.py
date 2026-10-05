"""covers: spec 0006 AC-15 (``close_abandoned``: positions left open by a ``completed`` or ``aborted``
run of this trader are closed at the newest snapshot's mark, or unpriced without one; their open
orders are canceled; one critical ``reconcile_mismatch`` each; the current run and other traders are
left alone; a second call changes nothing).

Runs as ``trader_app``, the role the trader starts with, so its grants are checked too.
"""

import time
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from sqlalchemy import Engine, create_engine, pool, select
from sqlalchemy.orm import Session, sessionmaker

from trading_core.db_roles import TRADER_ROLE
from trading_core.recording import models, repository
from trading_core.recording.enums import (
    Env,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RunMode,
    RunStatus,
    Severity,
    StrategyKind,
)
from trading_core.recording.rows import PositionSnapshotRow, ns_to_utc

from .conftest import World, instrument_row, role_url

NOW = 1_900_000_000_000_000_000


@pytest.fixture
def sessions(database: str, trading_env: None) -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(role_url(database, TRADER_ROLE), poolclass=pool.NullPool)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def version_id(sessions: sessionmaker[Session]) -> UUID:
    with sessions.begin() as session:
        repository.upsert_instrument(session, instrument_row())
        return repository.ensure_strategy_version(
            session, "placeholder_timed", StrategyKind.SCALPING, "0123456789abcdef", {"interval_min": 15}
        )


def _start(sessions, version_id, trader_id) -> World:
    with sessions.begin() as session:
        run_id = repository.start_run(
            session,
            env=Env.DEMO,
            mode=RunMode.PAPER,
            version_id=version_id,
            trader_id=trader_id,
            run_config={"fill_source": "quotes"},
            started_ns=time.time_ns(),
        )
    return World(version_id=version_id, run_id=run_id)


def _open_position(sessions, world: World, realized=Decimal("-0.04"), mark=None, unrealized=None):
    """An open long with a filled entry, a resting stop, and optionally snapshots (the newest last)."""
    decision = world.decision()
    entry = world.order(
        decision_id=decision.id,
        order_type=OrderType.MARKET,
        price=None,
        status=OrderStatus.FILLED,
        filled_qty=Decimal("0.001"),
    )
    stop = world.order(
        decision_id=decision.id,
        order_type=OrderType.STOP_MARKET,
        price=None,
        trigger_price=Decimal("59800"),
        side=OrderSide.SELL,
        reduce_only=True,
        status=OrderStatus.ACCEPTED,
    )
    position = world.position(realized_pnl=realized)
    with sessions.begin() as session:
        repository.record_decision(session, decision)
        repository.upsert_order(session, entry)
        repository.upsert_order(session, stop)
        repository.upsert_position(session, position)
        if mark is not None:
            for offset, (px, pnl) in enumerate([(Decimal("1"), Decimal("99")), (mark, unrealized)]):
                repository.record_position_snapshot(
                    session,
                    PositionSnapshotRow(
                        env=Env.DEMO,
                        position_id=position.id,
                        quantity=position.quantity,
                        mark_px=px,
                        unrealized_pnl=pnl,
                        currency="USD",
                        captured_ns=position.opened_ns + offset + 1,
                    ),
                )
    return position, entry, stop


def _close(sessions, trader_id, current_run_id):
    with sessions.begin() as session:
        return repository.close_abandoned(session, Env.DEMO, trader_id, current_run_id, NOW)


def _get(sessions, model, id_):
    with sessions() as session:
        return session.get(model, id_)


def _order(sessions, run_id, client_order_id):
    with sessions() as session:
        return session.execute(
            select(models.Order).where(models.Order.run_id == run_id, models.Order.client_order_id == client_order_id)
        ).scalar_one()


def test_positions_left_by_completed_and_aborted_runs_are_closed_and_nothing_else(sessions, version_id):
    trader = f"TRADER-{uuid7().hex[-8:]}"
    completed = _start(sessions, version_id, trader)
    priced, priced_entry, priced_stop = _open_position(
        sessions, completed, mark=Decimal("60100.5"), unrealized=Decimal("0.10")
    )
    with sessions.begin() as session:
        repository.finish_run(session, completed.run_id, RunStatus.COMPLETED, time.time_ns())
    aborted = _start(sessions, version_id, trader)  # left running: the next start marks it aborted
    unpriced, _, unpriced_stop = _open_position(sessions, aborted)
    other = _start(sessions, version_id, f"TRADER-{uuid7().hex[-8:]}")
    others, _, others_stop = _open_position(sessions, other)
    current = _start(sessions, version_id, trader)
    mine, _, mine_stop = _open_position(sessions, current)

    closed = _close(sessions, trader, current.run_id)

    assert set(closed) == {priced.id, unpriced.id}
    assert _get(sessions, models.StrategyRun, aborted.run_id).status == RunStatus.ABORTED

    row = _get(sessions, models.Position, priced.id)
    assert (row.status, row.side, row.quantity) == (PositionStatus.CLOSED, PositionSide.FLAT, 0)
    assert (row.closed_at, row.last_event_ns) == (ns_to_utc(NOW), NOW)
    assert row.avg_exit_px == Decimal("60100.5")  # the newest snapshot, not the older one
    assert row.realized_pnl == Decimal("0.06")  # -0.04 stored plus 0.10 unrealized
    assert row.fees_total == priced.fees_total

    row = _get(sessions, models.Position, unpriced.id)
    assert (row.status, row.side, row.quantity) == (PositionStatus.CLOSED, PositionSide.FLAT, 0)
    assert row.avg_exit_px is None
    assert row.realized_pnl == Decimal("-0.04")

    # Open orders of the earlier runs are canceled; filled ones and everyone else's stay.
    for world, stop in ((completed, priced_stop), (aborted, unpriced_stop)):
        order = _order(sessions, world.run_id, stop.client_order_id)
        assert (order.status, order.last_event_ns) == (OrderStatus.CANCELED, NOW)
    assert _order(sessions, completed.run_id, priced_entry.client_order_id).status == OrderStatus.FILLED
    assert _order(sessions, current.run_id, mine_stop.client_order_id).status == OrderStatus.ACCEPTED
    assert _order(sessions, other.run_id, others_stop.client_order_id).status == OrderStatus.ACCEPTED
    assert _get(sessions, models.Position, mine.id).status == PositionStatus.OPEN
    assert _get(sessions, models.Position, others.id).status == PositionStatus.OPEN

    with sessions() as session:
        events = (
            session.execute(select(models.RiskEvent).where(models.RiskEvent.event_key.like("abandoned:%")))
            .scalars()
            .all()
        )
    by_key = {e.event_key: e for e in events}
    assert set(by_key) == {f"abandoned:{priced.id}", f"abandoned:{unpriced.id}"}
    for event in events:
        assert (event.kind, event.severity) == (RiskEventKind.RECONCILE_MISMATCH, Severity.CRITICAL)
    assert by_key[f"abandoned:{priced.id}"].run_id == completed.run_id


def test_a_second_call_changes_nothing(sessions, version_id):
    trader = f"TRADER-{uuid7().hex[-8:]}"
    earlier = _start(sessions, version_id, trader)
    _open_position(sessions, earlier, mark=Decimal("60100.5"), unrealized=Decimal("0.10"))
    current = _start(sessions, version_id, trader)

    assert len(_close(sessions, trader, current.run_id)) == 1
    assert _close(sessions, trader, current.run_id) == []

    with sessions() as session:
        keys = session.execute(select(models.RiskEvent.event_key).where(models.RiskEvent.run_id == earlier.run_id))
        assert len(keys.all()) == 1


def test_another_env_is_refused(sessions, version_id):
    from trading_core.recording.errors import EnvMismatchError

    with sessions.begin() as session, pytest.raises(EnvMismatchError):
        repository.close_abandoned(session, Env.LIVE, "TRADER-LIVE", uuid7(), NOW)
