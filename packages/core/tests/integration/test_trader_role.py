"""covers: spec 0002 AC-11, the write API as the trader's own role (trader_app), not the owner."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import Engine, create_engine, pool, select
from sqlalchemy.orm import Session

from trading_core.db_roles import TRADER_ROLE, role_passwords
from trading_core.recording import models, repository
from trading_core.recording.enums import (
    ControlCmd,
    ControlOutcome,
    Env,
    OrderStatus,
    RunMode,
    RunStatus,
    StrategyKind,
)
from trading_core.recording.rows import ControlCommandRow

from .conftest import World, instrument_row, owner_settings, role_settings


@pytest.fixture
def trader_session(database: str, trading_env: None) -> Iterator[Session]:
    settings = owner_settings(database).model_copy(
        update={"postgres_user": TRADER_ROLE, "postgres_password": role_passwords(role_settings())[TRADER_ROLE]}
    )
    engine: Engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with Session(engine) as db:
        yield db
        db.rollback()
    engine.dispose()


def test_the_trader_role_can_run_the_whole_paper_write_path(trader_session: Session):
    session = trader_session
    repository.upsert_instrument(session, instrument_row())
    version_id = repository.ensure_strategy_version(
        session, "trader-role", StrategyKind.SCALPING, "0123456789abcdef", {"window": 20}
    )
    run_id = repository.start_run(
        session,
        env=Env.DEMO,
        mode=RunMode.PAPER,
        version_id=version_id,
        trader_id=f"TRADER-{uuid7().hex[-8:]}",
        run_config={},
        started_ns=1_800_000_000_000_000_000,
    )
    world = World(version_id=version_id, run_id=run_id)

    decision = world.decision()
    repository.record_decision(session, decision)
    order = world.order(decision_id=decision.id)
    repository.upsert_order(session, order)
    repository.upsert_order(session, replace(order, status=OrderStatus.FILLED, last_event_ns=world.tick()))
    fill = world.fill(order.client_order_id)
    repository.record_fill(session, fill)
    position = world.position()
    repository.upsert_position(session, position)
    repository.upsert_position(session, replace(position, quantity=Decimal(0), last_event_ns=world.tick()))
    session.flush()
    position_id = session.execute(
        select(models.Position.id).where(models.Position.position_ref == position.position_ref)
    ).scalar_one()
    repository.record_fill(session, replace(fill, id=uuid7(), position_id=position_id))  # fills NULL position_id

    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    command = ControlCommandRow(
        id=uuid7(), env=Env.DEMO, cmd=ControlCmd.STOP, issued_at=now, expires_at=now, received_at=now
    )
    repository.record_control_command(session, command)
    repository.set_control_outcome(session, command.id, ControlOutcome.EXECUTED, world.tick())
    repository.finish_run(session, run_id, RunStatus.COMPLETED, world.tick())
    session.flush()

    stored_order = session.execute(select(models.Order.status).where(models.Order.id == order.id)).scalar_one()
    stored_fill = session.execute(
        select(models.Fill.position_id).where(models.Fill.run_id == run_id, models.Fill.trade_id == fill.trade_id)
    ).scalar_one()
    assert (stored_order, stored_fill) == (OrderStatus.FILLED, position_id)
