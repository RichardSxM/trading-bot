"""covers: spec 0006 AC-11 (a round trip's rows through the writer, fills linked to their position),
AC-12 (a conflict is dead lettered and blocks entries; a bad item does not stop the rest),
AC-13 (a spill replays with nothing new and is deleted).

The writer runs as ``trader_app``, the role the trader uses, on its own connections, so the run
it writes under is committed first.
"""

import time
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid7

import pytest
from sqlalchemy import Engine, create_engine, func, pool, select
from sqlalchemy.orm import Session, sessionmaker

from trading_core.db_roles import TRADER_ROLE
from trading_core.recording import models, repository
from trading_core.recording.enums import (
    Env,
    OrderEventKind,
    OrderStatus,
    PositionStatus,
    RiskEventKind,
    RunMode,
    Severity,
    StrategyKind,
)
from trading_core.recording.rows import (
    AccountSnapshotRow,
    OrderEventRow,
    PositionSnapshotRow,
    RiskEventRow,
)
from trading_core.recording.spool import WriteCall, WriteItem, append, read
from trading_core.recording.writer import RecordingWriter, deadletter_path, replay_spool, spool_path
from trading_core.strategy.blocks import BlockReason, EntryBlocks

from .conftest import World, instrument_row, role_url


@pytest.fixture
def sessions(database: str, trading_env: None) -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(role_url(database, TRADER_ROLE), poolclass=pool.NullPool)
    yield sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def run(sessions: sessionmaker[Session]) -> World:
    with sessions.begin() as session:
        repository.upsert_instrument(session, instrument_row())
        version_id = repository.ensure_strategy_version(
            session, "placeholder_timed", StrategyKind.SCALPING, "0123456789abcdef", {"interval_min": 15}
        )
        run_id = repository.start_run(
            session,
            env=Env.DEMO,
            mode=RunMode.PAPER,
            version_id=version_id,
            trader_id=f"TRADER-{uuid7().hex[-8:]}",
            run_config={"fill_source": "quotes"},
            started_ns=time.time_ns(),
        )
    return World(version_id=version_id, run_id=run_id)


def _round_trip(world: World) -> list[WriteItem]:
    """What the actor and the strategy queue for one entry fill: in the order they queue it."""
    decision = world.decision()
    order = world.order(decision_id=decision.id, status=OrderStatus.SUBMITTED)
    filled = replace(order, status=OrderStatus.FILLED, filled_qty=order.quantity, last_event_ns=world.tick())
    fill = world.fill(order.client_order_id)
    position = world.position(opened_ns=fill.filled_ns, last_event_ns=fill.filled_ns)
    event = OrderEventRow(
        env=Env.DEMO,
        run_id=world.run_id,
        client_order_id=order.client_order_id,
        event_id=uuid7(),
        kind=OrderEventKind.ORDER_FILLED,
        details={"trade_id": fill.trade_id},
        occurred_ns=fill.filled_ns,
    )
    return [
        WriteItem(WriteCall.UPSERT_INSTRUMENT, instrument_row()),
        WriteItem(WriteCall.RECORD_DECISION, decision),
        WriteItem(WriteCall.UPSERT_ORDER, order),
        WriteItem(WriteCall.UPSERT_ORDER, filled),
        WriteItem(WriteCall.RECORD_ORDER_EVENT, event),
        WriteItem(WriteCall.RECORD_FILL, fill),
        WriteItem(WriteCall.UPSERT_POSITION, position),
        WriteItem(WriteCall.RECORD_FILL, replace(fill, position_id=position.id)),
        WriteItem(
            WriteCall.RECORD_POSITION_SNAPSHOT,
            PositionSnapshotRow(
                env=Env.DEMO,
                position_id=position.id,
                quantity=position.quantity,
                mark_px=Decimal("60010.5"),
                unrealized_pnl=Decimal("0.01"),
                currency="USD",
                captured_ns=world.tick(),
            ),
        ),
        WriteItem(
            WriteCall.RECORD_ACCOUNT_SNAPSHOT,
            AccountSnapshotRow(
                env=Env.DEMO,
                run_id=world.run_id,
                account_ref="KRAKEN-001",
                balance_total=Decimal(1000),
                balance_free=Decimal(990),
                margin_used=Decimal(10),
                currency="USD",
                captured_ns=world.clock,
            ),
        ),
    ]


def _write(sessions, items: list[WriteItem], deadletter: Path, blocks: EntryBlocks) -> RecordingWriter:
    writer = RecordingWriter(sessions, blocks, queue_max=100, max_backlog_s=30.0, deadletter=deadletter)
    writer.start()
    for item in items:
        writer.put(item)
    assert writer.drain(10.0)
    return writer


def _count(sessions, model, **where) -> int:
    with sessions() as session:
        query = select(func.count()).select_from(model)
        for name, value in where.items():
            query = query.where(getattr(model, name) == value)
        return int(session.execute(query).scalar_one())


def test_a_round_trip_lands_through_the_writer_with_the_fill_linked_to_its_position(sessions, run, tmp_path):
    blocks = EntryBlocks()
    items = _round_trip(run)

    _write(sessions, items, deadletter_path(tmp_path, run.run_id), blocks)

    assert blocks.active() == ()
    assert not deadletter_path(tmp_path, run.run_id).exists()
    with sessions() as session:
        order = session.execute(select(models.Order).where(models.Order.run_id == run.run_id)).scalar_one()
        assert order.status == OrderStatus.FILLED
        position = session.execute(select(models.Position).where(models.Position.run_id == run.run_id)).scalar_one()
        assert position.status == PositionStatus.OPEN
        fill = session.execute(select(models.Fill).where(models.Fill.order_id == order.id)).scalar_one()
        assert fill.position_id == position.id
    assert _count(sessions, models.PositionSnapshot, position_id=position.id) == 1
    assert _count(sessions, models.AccountSnapshot, run_id=run.run_id) == 1


def test_a_spill_replays_with_nothing_new_and_is_deleted(sessions, run, tmp_path):
    items = _round_trip(run)
    _write(sessions, items, deadletter_path(tmp_path, run.run_id), EntryBlocks())
    append(spool_path(tmp_path, run.run_id), items)  # as if the stop had spilled them unwritten

    blocks = EntryBlocks()
    assert replay_spool(tmp_path, sessions, blocks) == len(items)

    assert not spool_path(tmp_path, run.run_id).exists()
    assert blocks.active() == ()
    assert _count(sessions, models.Order, run_id=run.run_id) == 1
    assert _count(sessions, models.Fill, run_id=run.run_id) == 1


def test_a_conflicting_risk_event_is_dead_lettered_and_blocks_and_the_rest_still_lands(sessions, run, tmp_path):
    key = f"limit_hit:{uuid7()}"
    first = RiskEventRow(
        env=Env.DEMO,
        event_key=key,
        kind=RiskEventKind.LIMIT_HIT,
        severity=Severity.INFO,
        message="entry refused: paused",
        details={"reason": "paused"},
        run_id=run.run_id,
        occurred_ns=run.tick(),
    )
    contradicting = replace(first, message="entry refused: notional_cap", details={"reason": "notional_cap"})
    after = _round_trip(run)
    deadletter = deadletter_path(tmp_path, run.run_id)
    blocks = EntryBlocks()

    _write(
        sessions,
        [WriteItem(WriteCall.RECORD_RISK_EVENT, first), WriteItem(WriteCall.RECORD_RISK_EVENT, contradicting), *after],
        deadletter,
        blocks,
    )

    assert BlockReason.RECORD_CONFLICT in blocks.active()
    assert [item.row for item in read(deadletter)] == [contradicting]
    assert _count(sessions, models.Fill, run_id=run.run_id) == 1  # the writer kept going


def test_a_dead_letter_file_is_never_replayed(sessions, run, tmp_path):
    items = _round_trip(run)
    append(deadletter_path(tmp_path, run.run_id), items)

    assert replay_spool(tmp_path, sessions, EntryBlocks()) == 0
    assert deadletter_path(tmp_path, run.run_id).exists()
    assert _count(sessions, models.Order, run_id=run.run_id) == 0
