"""covers: spec 0002 AC-2 to AC-8, the traceable write thread from instrument to fill."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_core.recording import models, repository
from trading_core.recording.enums import (
    Env,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskEventKind,
    RunMode,
    RunStatus,
    Severity,
    StrategyKind,
)
from trading_core.recording.errors import (
    EnvMismatchError,
    InvalidTransitionError,
    MissingParentError,
    RecordConflictError,
)

from .conftest import INSTRUMENT, World, instrument_row

TRACE = text(
    """
    SELECT f.trade_id, o.client_order_id, d.reason, r.mode, v.git_commit, v.config
    FROM fills f
    JOIN orders o ON o.id = f.order_id AND o.env = f.env
    JOIN decisions d ON d.id = o.decision_id AND d.env = o.env
    JOIN strategy_runs r ON r.id = d.run_id AND r.env = d.env
    JOIN strategy_versions v ON v.id = r.strategy_version_id
    WHERE f.trade_id = :trade_id
    """
)


def _count(session: Session, model, **where) -> int:
    clauses = [getattr(model, name) == value for name, value in where.items()]
    return session.execute(select(func.count()).select_from(model).where(*clauses)).scalar_one()


def test_tracer_fill_traces_to_order_decision_run_and_version(session: Session, world: World):
    decision = world.decision()
    repository.record_decision(session, decision)
    order = world.order(decision_id=decision.id)
    repository.upsert_order(session, order)
    fill = world.fill(order.client_order_id, price=Decimal("98765.4321"), quantity=Decimal("0.00000001"))
    repository.record_fill(session, fill)
    repository.upsert_position(session, world.position())
    session.commit()

    trace = session.execute(TRACE, {"trade_id": fill.trade_id}).mappings().one()

    assert trace["client_order_id"] == order.client_order_id
    assert trace["reason"] == decision.reason
    assert trace["mode"] == RunMode.PAPER
    assert trace["git_commit"] == "0123456789abcdef"
    assert trace["config"] == {"size": "0.001", "window": 20}
    stored = session.execute(select(models.Fill).where(models.Fill.trade_id == fill.trade_id)).scalar_one()
    assert (stored.price, stored.quantity, stored.filled_ns) == (
        Decimal("98765.4321"),
        Decimal("0.00000001"),
        fill.filled_ns,
    )
    assert stored.filled_at == fill.filled_at


def test_the_same_writes_twice_leave_one_row_each(session: Session, world: World):
    decision = world.decision()
    order = world.order(decision_id=decision.id)
    fill = world.fill(order.client_order_id)
    for _ in range(2):
        repository.upsert_instrument(session, instrument_row())
        repository.record_decision(session, replace(decision, id=decision.id))
        repository.upsert_order(session, replace(order, id=uuid7()))
        repository.record_fill(session, replace(fill, id=uuid7()))

    assert _count(session, models.Decision, id=decision.id) == 1
    assert _count(session, models.Order, run_id=world.run_id, client_order_id=order.client_order_id) == 1
    assert _count(session, models.Fill, run_id=world.run_id, trade_id=fill.trade_id) == 1


def test_ensure_strategy_version_is_idempotent_on_the_hash(session: Session):
    config = {"window": 5}
    first = repository.ensure_strategy_version(session, "idem", StrategyKind.SCALPING, "abc", config)
    second = repository.ensure_strategy_version(session, "idem", StrategyKind.SCALPING, "abc", config)
    other = repository.ensure_strategy_version(session, "idem", StrategyKind.SCALPING, "abd", config)

    assert first == second != other
    with pytest.raises(RecordConflictError):
        repository.ensure_strategy_version(session, "idem", StrategyKind.HOUR_LEVEL, "abc", config)


def test_a_conflicting_fill_raises_keeps_the_original_and_is_reported_once(engine, session: Session, world: World):
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    fill = world.fill(order.client_order_id)
    repository.record_fill(session, fill)
    session.commit()

    with pytest.raises(RecordConflictError) as caught:
        repository.record_fill(session, replace(fill, id=uuid7(), price=fill.price + 1))
    session.rollback()

    assert (
        session.execute(select(models.Fill.price).where(models.Fill.trade_id == fill.trade_id)).scalar_one()
        == fill.price
    )
    for _ in range(2):
        with Session(engine) as fresh:
            repository.report_conflict(fresh, caught.value)
    with Session(engine) as check:
        reported = (
            check.execute(
                select(models.RiskEvent).where(models.RiskEvent.event_key.startswith("record_conflict:fills:")),
            )
            .scalars()
            .all()
        )
    mine = [event for event in reported if fill.trade_id in event.event_key]
    assert len(mine) == 1
    assert (mine[0].kind, mine[0].severity, mine[0].env) == (RiskEventKind.RECORD_CONFLICT, Severity.CRITICAL, Env.DEMO)


def test_a_fill_before_its_order_creates_a_stub_that_the_order_fills_in(session: Session, world: World):
    fill = world.fill("O-late")
    repository.record_fill(session, fill)

    stub = session.execute(select(models.Order).where(models.Order.client_order_id == "O-late")).scalar_one()
    assert (stub.origin, stub.status, stub.last_event_ns, stub.quantity) == ("external", "unknown", 0, fill.quantity)
    orphan = session.execute(
        select(models.RiskEvent).where(models.RiskEvent.event_key == f"orphan_fill:{world.run_id}:{fill.trade_id}")
    ).scalar_one()
    assert orphan.kind == RiskEventKind.ORPHAN_FILL

    decision = world.decision()
    repository.record_decision(session, decision)
    repository.upsert_order(
        session, world.order(client_order_id="O-late", decision_id=decision.id, status=OrderStatus.FILLED)
    )
    session.flush()
    session.refresh(stub)
    assert (stub.origin, stub.status, stub.decision_id, stub.order_type) == ("strategy", "filled", decision.id, "limit")


def test_a_fill_after_a_restart_finds_the_order_from_the_previous_run(session: Session, world: World):
    first_run = session.get(models.StrategyRun, world.run_id)
    assert first_run is not None
    trader_id = first_run.trader_id
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    new_run = repository.start_run(
        session,
        env=Env.DEMO,
        mode=RunMode.PAPER,
        version_id=world.version_id,
        trader_id=trader_id,
        run_config={},
        started_ns=world.tick(),
    )

    repository.record_fill(session, world.fill(order.client_order_id, run_id=new_run))

    assert _count(session, models.Order, client_order_id=order.client_order_id) == 1
    session.refresh(first_run)
    assert first_run.status == RunStatus.ABORTED


def test_an_older_order_update_is_ignored_and_a_repeat_changes_nothing(session: Session, world: World):
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION, status=OrderStatus.FILLED)
    repository.upsert_order(session, order)
    repository.upsert_order(session, replace(order, status=OrderStatus.ACCEPTED, last_event_ns=order.last_event_ns - 1))
    repository.upsert_order(session, order)

    stored = session.execute(
        select(models.Order).where(models.Order.client_order_id == order.client_order_id)
    ).scalar_one()
    assert (stored.status, stored.last_event_ns) == ("filled", order.last_event_ns)


def test_an_order_may_not_change_side_or_lose_its_decision_once_real(session: Session, world: World):
    decision = world.decision()
    repository.record_decision(session, decision)
    order = world.order(decision_id=decision.id)
    repository.upsert_order(session, order)

    with pytest.raises(RecordConflictError):
        repository.upsert_order(session, replace(order, side=OrderSide.SELL, last_event_ns=world.tick()))
    with pytest.raises(RecordConflictError):
        repository.upsert_order(session, replace(order, order_type=OrderType.MARKET, last_event_ns=world.tick()))


def test_an_order_update_with_the_same_last_event_ns_applies(session: Session, world: World):
    # covers: AC-8 (greater than or equal applies)
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    repository.upsert_order(session, replace(order, status=OrderStatus.CANCELED))

    stored = session.execute(
        select(models.Order).where(models.Order.client_order_id == order.client_order_id)
    ).scalar_one()
    assert (stored.status, stored.last_event_ns) == ("canceled", order.last_event_ns)


def test_a_newer_order_update_moves_the_order_forward(session: Session, world: World):
    # covers: AC-8
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    newer = replace(order, status=OrderStatus.FILLED, last_event_ns=world.tick())
    repository.upsert_order(session, newer)

    stored = session.execute(
        select(models.Order).where(models.Order.client_order_id == order.client_order_id)
    ).scalar_one()
    assert (stored.status, stored.last_event_ns) == ("filled", newer.last_event_ns)


def test_an_older_position_update_is_ignored(session: Session, world: World):
    position = world.position(last_event_ns=world.tick())
    repository.upsert_position(session, position)
    repository.upsert_position(
        session, replace(position, quantity=Decimal(0), last_event_ns=position.last_event_ns - 1)
    )

    stored = session.execute(
        select(models.Position).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    assert stored.quantity == position.quantity


def test_the_database_rejects_a_strategy_order_without_a_decision(session: Session, world: World):
    with pytest.raises(IntegrityError, match="strategy_order_has_decision"):
        repository.upsert_order(session, world.order(decision_id=None, origin=OrderOrigin.STRATEGY))


@pytest.mark.parametrize("origin", [OrderOrigin.KILL_SWITCH, OrderOrigin.RECONCILIATION, OrderOrigin.EXTERNAL])
def test_non_strategy_orders_may_omit_the_decision(session: Session, world: World, origin):
    repository.upsert_order(session, world.order(decision_id=None, origin=origin))


def test_the_database_rejects_a_child_in_another_env_than_its_parent(session: Session, world: World):
    order = world.order(decision_id=None, origin=OrderOrigin.RECONCILIATION)
    repository.upsert_order(session, order)
    session.flush()

    with pytest.raises(IntegrityError, match="fk_"):
        session.execute(
            text(
                "INSERT INTO fills (id, env, run_id, order_id, instrument_id, trade_id, side, quantity, price, fee,"
                " fee_currency, liquidity, filled_at, filled_ns) VALUES (:id, 'live', :run, :order, :inst, 'T-x',"
                " 'buy', 1, 1, 0, 'USD', 'maker', now(), 1)"
            ),
            {"id": uuid7(), "run": world.run_id, "order": order.id, "inst": INSTRUMENT},
        )


def test_the_database_rejects_a_live_run_in_demo(session: Session, world: World):
    with pytest.raises(IntegrityError, match="live_mode_in_live_env"):
        session.execute(
            text(
                "INSERT INTO strategy_runs (id, env, mode, status, strategy_version_id, trader_id, run_config,"
                " started_at) VALUES (:id, 'demo', 'live', 'running', :version, 'T', '{}', now())"
            ),
            {"id": uuid7(), "version": world.version_id},
        )


def test_the_database_limits_env_to_demo_or_live(session: Session, world: World):
    # covers: AC-4
    with pytest.raises(IntegrityError, match="ck_strategy_runs_env_valid"):
        session.execute(
            text(
                "INSERT INTO strategy_runs (id, env, mode, status, strategy_version_id, trader_id, run_config,"
                " started_at) VALUES (:id, 'paper', 'paper', 'running', :version, 'T', '{}', now())"
            ),
            {"id": uuid7(), "version": world.version_id},
        )


def test_a_fill_for_an_unknown_run_is_refused_without_a_stub(session: Session, world: World):
    fill = world.fill("O-nowhere", run_id=uuid7())

    with pytest.raises(MissingParentError):
        repository.record_fill(session, fill)

    assert _count(session, models.Order, client_order_id="O-nowhere") == 0


def test_finish_run_refuses_running_and_an_unknown_run(session: Session, world: World):
    with pytest.raises(InvalidTransitionError):
        repository.finish_run(session, world.run_id, RunStatus.RUNNING, world.tick())
    with pytest.raises(MissingParentError):
        repository.finish_run(session, uuid7(), RunStatus.COMPLETED, world.tick())

    run = session.get(models.StrategyRun, world.run_id)
    assert run is not None
    assert run.status == RunStatus.RUNNING


def test_the_write_api_rejects_a_row_from_the_other_env(session: Session, world: World):
    with pytest.raises(EnvMismatchError):
        repository.record_decision(session, world.decision(env=Env.LIVE))


def test_finish_run_moves_running_to_a_terminal_status_once(session: Session, world: World):
    repository.finish_run(session, world.run_id, RunStatus.COMPLETED, world.tick())

    run = session.get(models.StrategyRun, world.run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    with pytest.raises(InvalidTransitionError):
        repository.finish_run(session, world.run_id, RunStatus.FAILED, world.tick())
