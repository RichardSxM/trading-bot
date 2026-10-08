"""The recording write API (spec 0002): idempotent, env checked writes over a sync ``Session``.

Every function takes a row from ``trading_core.recording.rows``. The caller owns commit and
rollback, except ``report_conflict`` and ``write_run_bundle``, which commit themselves. A
repeated write with equal content is a no-op; one with different content raises
``RecordConflictError`` and leaves the stored row unchanged. The caller then rolls back,
calls ``report_conflict`` with a fresh session, and fails closed.
"""

import hashlib
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, Table, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_core.db import Base
from trading_core.recording import models
from trading_core.recording.enums import (
    CLOSED_ORDER_STATUSES,
    SIM_MODES,
    ControlOutcome,
    Env,
    FundingSource,
    OrderOrigin,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RunMode,
    RunStatus,
    Severity,
    Stage,
    StrategyKind,
    TimeInForce,
    Verdict,
)
from trading_core.recording.errors import (
    EnvMismatchError,
    ForeignRunError,
    InvalidModeError,
    InvalidTransitionError,
    MissingEvidenceError,
    MissingParentError,
    PromotionRequiredError,
    RecordConflictError,
    UnknownCommitError,
)
from trading_core.recording.rows import (
    AccountSnapshotRow,
    ControlCommandRow,
    DecisionRow,
    FillRow,
    FundingRow,
    InstrumentRow,
    JsonObject,
    JsonValue,
    OrderEventRow,
    OrderRow,
    PositionRow,
    PositionSnapshotRow,
    PromotionRow,
    RiskEventRow,
    RunBundle,
    RunFillStatsRow,
    RunResultRow,
    RunRow,
    canonical_json,
    column_values,
    normalize_json,
    ns_to_utc,
    version_hash,
)
from trading_core.settings import get_settings

_BOOKKEEPING: Final = frozenset({"id", "created_at", "updated_at"})
# May go once from NULL to a value; NULL on either side counts as equal.
_FILL_ONCE: Final = "position_id"
_ORDER_IMMUTABLE: Final = ("side", "instrument_id", "client_order_id")
_ORDER_STUB_ONLY: Final = ("origin", "decision_id", "order_type")
_POSITION_IMMUTABLE: Final = ("instrument_id", "position_ref", "opened_ns")
_LIVE_GATE: Final = (Stage.PAPER, Stage.LIVE)
# The trader's handling state of a control command; a redelivered command compares without it.
_CONTROL_HANDLING: Final = frozenset({"received_at", "outcome", "outcome_at", "details"})
_FUNDING_KEYS: Final = {
    FundingSource.KRAKEN_LOG: (("env", "external_id"), "source = 'kraken_log'"),
    FundingSource.COMPUTED: (("run_id", "instrument_id", "paid_ns"), "source = 'computed'"),
}


# Shared helpers ---------------------------------------------------------------------------


def _table(model: type[Base]) -> Table:
    return Base.metadata.tables[model.__tablename__]


def _check_env(env: str) -> None:
    expected = get_settings().trading_env
    if env != expected:
        raise EnvMismatchError(f"row env {env!r} differs from TRADING_ENV={expected!r}")


def _content(values: Mapping[str, object]) -> dict[str, object]:
    """Comparable content: no bookkeeping columns, and times compared by their ``_ns`` value."""
    return {
        name: value
        for name, value in values.items()
        if name not in _BOOKKEEPING and not (name.endswith("_at") and name.removesuffix("_at") + "_ns" in values)
    }


def _key_filter(table: Table, key: Mapping[str, object], where: str | None = None) -> ColumnElement[bool]:
    # ``=`` and ``IS NULL`` both use the btree index; ``IS NOT DISTINCT FROM`` would not.
    clauses = [table.c[name].is_(None) if value is None else table.c[name] == value for name, value in key.items()]
    if where is None:
        return and_(*clauses)
    return and_(*clauses, text(where))


def _stored(
    session: Session, table: Table, key: Mapping[str, object], where: str | None = None, *, lock: bool = False
) -> dict[str, object] | None:
    query = select(table).where(_key_filter(table, key, where))
    if lock:
        query = query.with_for_update()
    row = session.execute(query).mappings().one_or_none()
    return None if row is None else {str(name): value for name, value in row.items()}


def _differences(
    stored: Mapping[str, object], incoming: Mapping[str, object], ignore: frozenset[str] = frozenset()
) -> list[str]:
    stored_content, incoming_content = _content(stored), _content(incoming)
    differing = []
    for name, value in incoming_content.items():
        kept = stored_content.get(name)
        if name in ignore:
            continue
        if name == _FILL_ONCE and (kept is None or value is None):
            continue
        if kept != value:
            differing.append(name)
    return differing


def _conflict(
    table: Table, key: Mapping[str, object], stored: Mapping[str, object], incoming: Mapping[str, object]
) -> RecordConflictError:
    return RecordConflictError(table.name, key, _content(stored), _content(incoming))


def _insert_or_compare(
    session: Session,
    model: type[Base],
    values: Mapping[str, object],
    key: Sequence[str],
    where: str | None = None,
    ignore: frozenset[str] = frozenset(),
) -> bool:
    """INSERT … ON CONFLICT (key) DO NOTHING; on a conflict compare with the stored row.

    Returns True when a row was inserted. Equal content is a no-op (a stored NULL
    ``position_id`` is then set from the incoming value); different content raises
    ``RecordConflictError``. ``where`` names the partial unique index to arbitrate on;
    ``ignore`` lists mutable columns left out of the comparison.
    """
    table = _table(model)
    stmt = insert(table).values(dict(values))
    if where is None:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(key))
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(key), index_where=text(where))
    if session.execute(stmt.returning(table.c[key[0]])).first() is not None:
        return True

    key_values = {name: values[name] for name in key}
    stored = _stored(session, table, key_values, where)
    assert stored is not None  # the insert hit this key, so the row exists
    if _differences(stored, values, ignore):
        raise _conflict(table, key_values, stored, values)
    if _FILL_ONCE in values and stored.get(_FILL_ONCE) is None and values[_FILL_ONCE] is not None:
        session.execute(
            update(table).where(_key_filter(table, key_values, where)).values({_FILL_ONCE: values[_FILL_ONCE]})
        )
    return False


def _guarded_upsert(
    session: Session,
    model: type[Base],
    values: Mapping[str, object],
    key: Sequence[str],
    immutable: Sequence[str],
    stub_only: Sequence[str] = (),
) -> None:
    """INSERT … ON CONFLICT (key) DO UPDATE … WHERE stored.last_event_ns <= excluded.last_event_ns.

    ``immutable`` fields never change; ``stub_only`` fields may change only while the stored
    status is ``unknown``. A breach raises ``RecordConflictError`` before anything is written.
    The stored row stays locked until the caller commits, so a concurrent writer cannot slip
    past the check.
    """
    table = _table(model)
    key_values = {name: values[name] for name in key}
    stored = _stored(session, table, key_values, lock=True)
    if stored is not None:
        frozen = list(immutable)
        if stub_only and stored["status"] != OrderStatus.UNKNOWN:
            frozen += stub_only
        if [name for name in frozen if stored[name] != values[name]]:
            raise _conflict(table, key_values, stored, values)

    stmt = insert(table).values(dict(values))
    changed: dict[str, object] = {
        name: stmt.excluded[name] for name in values if name not in _BOOKKEEPING and name not in key
    }
    changed["updated_at"] = func.now()
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=list(key),
            set_=changed,
            where=table.c.last_event_ns <= stmt.excluded.last_event_ns,
        )
    )


def _json_value(value: object) -> JsonValue:
    """A value as plain JSON for risk event details (ids and times as strings)."""
    if isinstance(value, str | int | bool | Decimal) or value is None:
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    return str(value)


# Global records ---------------------------------------------------------------------------


def upsert_instrument(session: Session, row: InstrumentRow) -> None:
    """Insert an instrument, or do nothing when it is stored with equal specs.

    Raises ``RecordConflictError`` when any spec differs: a changed instrument needs a
    deliberate update, so the trader fails closed.
    """
    _insert_or_compare(session, models.Instrument, column_values(row), ["instrument_id"])


def ensure_strategy_version(
    session: Session, name: str, kind: StrategyKind, git_commit: str | None, config: JsonObject
) -> UUID:
    """Return the id of the version with this commit and config, creating strategy and version if new.

    ``git_commit=None`` takes the commit the image was built from (``Settings.git_commit``).
    Raises ``RecordConflictError`` when the strategy name exists with another kind, or the
    version hash already belongs to another strategy.
    """
    if git_commit is None:
        git_commit = get_settings().git_commit
    strategies = _table(models.Strategy)
    session.execute(
        insert(strategies).values(id=uuid.uuid7(), name=name, kind=kind).on_conflict_do_nothing(index_elements=["name"])
    )
    strategy = session.execute(select(models.Strategy).where(models.Strategy.name == name)).scalar_one()
    if strategy.kind != kind:
        raise RecordConflictError(strategies.name, {"name": name}, {"kind": strategy.kind}, {"kind": kind})

    versions = _table(models.StrategyVersion)
    digest = version_hash(git_commit, config)
    values = {
        "id": uuid.uuid7(),
        "strategy_id": strategy.id,
        "git_commit": git_commit,
        "config": normalize_json(config),
        "version_hash": digest,
    }
    session.execute(insert(versions).values(values).on_conflict_do_nothing(index_elements=["version_hash"]))
    version = session.execute(
        select(models.StrategyVersion).where(models.StrategyVersion.version_hash == digest)
    ).scalar_one()
    if version.strategy_id != strategy.id:
        raise RecordConflictError(
            versions.name, {"version_hash": digest}, {"strategy_id": version.strategy_id}, {"strategy_id": strategy.id}
        )
    return version.id


# Runs -------------------------------------------------------------------------------------


def _require_live_gate(session: Session, version_id: UUID) -> None:
    version = session.get(models.StrategyVersion, version_id)
    if version is None:
        raise MissingParentError(f"strategy version {version_id} not found")
    if version.git_commit == "unknown":
        raise UnknownCommitError(f"strategy version {version_id} has git_commit 'unknown'; a live run needs a commit")
    latest = session.execute(
        select(models.Promotion.verdict)
        .where(
            models.Promotion.strategy_version_id == version_id,
            models.Promotion.from_stage == _LIVE_GATE[0],
            models.Promotion.to_stage == _LIVE_GATE[1],
        )
        .order_by(models.Promotion.decided_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest != Verdict.PASS:
        raise PromotionRequiredError(
            f"strategy version {version_id}: latest paper → live promotion is {latest or 'missing'}, not pass"
        )


def start_run(
    session: Session,
    *,
    env: Env,
    mode: RunMode,
    version_id: UUID,
    trader_id: str,
    run_config: JsonObject,
    started_ns: int,
    data_start_at: datetime | None = None,
    data_end_at: datetime | None = None,
) -> UUID:
    """Start a run and return its id; first marks any ``running`` run of this env and trader ``aborted``.

    A live run needs a known commit (``UnknownCommitError``) and a latest ``paper → live``
    promotion with verdict ``pass`` (``PromotionRequiredError``).
    """
    _check_env(env)
    if mode == RunMode.LIVE:
        _require_live_gate(session, version_id)
    row = RunRow(
        env=env,
        mode=mode,
        strategy_version_id=version_id,
        trader_id=trader_id,
        run_config=run_config,
        data_start_at=data_start_at,
        data_end_at=data_end_at,
        started_at=ns_to_utc(started_ns),
    )
    session.execute(
        update(models.StrategyRun)
        .where(
            models.StrategyRun.env == env,
            models.StrategyRun.trader_id == trader_id,
            models.StrategyRun.status == RunStatus.RUNNING,
        )
        .values(status=RunStatus.ABORTED, ended_at=row.started_at, updated_at=func.now())
    )
    session.execute(insert(_table(models.StrategyRun)).values(column_values(row)))
    return row.id


def finish_run(session: Session, run_id: UUID, status: RunStatus, ended_ns: int) -> None:
    """Move a ``running`` run to a terminal status; anything else raises ``InvalidTransitionError``."""
    if status == RunStatus.RUNNING:
        raise InvalidTransitionError("a run cannot finish as running")
    run = session.get(models.StrategyRun, run_id)
    if run is None:
        raise MissingParentError(f"run {run_id} not found")
    _check_env(run.env)
    finished = session.execute(
        update(models.StrategyRun)
        .where(models.StrategyRun.id == run_id, models.StrategyRun.status == RunStatus.RUNNING)
        .values(status=status, ended_at=ns_to_utc(ended_ns), updated_at=func.now())
        .returning(models.StrategyRun.id)
    ).first()
    if finished is None:
        raise InvalidTransitionError(f"run {run_id} is {run.status}, not running")


def close_abandoned(session: Session, env: Env, trader_id: str, current_run_id: UUID, now_ns: int) -> list[UUID]:
    """Close what earlier runs of this trader left open; returns the closed position ids (spec 0006 AC-15).

    Demo only: the sandbox forgets its positions on a restart, so a position still ``open`` in any
    other run of this env and ``trader_id`` (whatever that run's status) can never be closed by a
    fill. It is closed flat at the newest snapshot's mark, with that snapshot's unrealized P&L
    added to its realized P&L; without a snapshot the exit price stays NULL and the P&L unchanged.
    Each gets one critical ``reconcile_mismatch`` risk event keyed ``abandoned:<position_id>``.
    Every order those runs left open is ``canceled``. Calling it again changes nothing.
    """
    _check_env(env)
    earlier_runs = select(models.StrategyRun.id).where(
        models.StrategyRun.env == env,
        models.StrategyRun.trader_id == trader_id,
        models.StrategyRun.id != current_run_id,
    )
    positions = (
        session.execute(
            select(models.Position).where(
                models.Position.env == env,
                models.Position.status == PositionStatus.OPEN,
                models.Position.run_id.in_(earlier_runs),
            )
        )
        .scalars()
        .all()
    )
    closed: list[UUID] = []
    for position in positions:
        snapshot = session.execute(
            select(models.PositionSnapshot)
            .where(models.PositionSnapshot.position_id == position.id)
            .order_by(models.PositionSnapshot.captured_ns.desc())
            .limit(1)
        ).scalar_one_or_none()
        values: dict[str, object] = {
            "status": PositionStatus.CLOSED,
            "side": PositionSide.FLAT,
            "quantity": Decimal(0),
            "closed_at": ns_to_utc(now_ns),
            "last_event_ns": now_ns,
            "updated_at": func.now(),
        }
        if snapshot is not None:
            values |= {
                "avg_exit_px": snapshot.mark_px,
                "realized_pnl": position.realized_pnl + snapshot.unrealized_pnl,
            }
        session.execute(update(models.Position).where(models.Position.id == position.id).values(values))
        record_risk_event(
            session,
            RiskEventRow(
                env=env,
                event_key=f"abandoned:{position.id}",
                kind=RiskEventKind.RECONCILE_MISMATCH,
                severity=Severity.CRITICAL,
                message="position left open by an earlier run closed as abandoned",
                details={
                    "position_id": str(position.id),
                    "current_run_id": str(current_run_id),
                    "mark_px": None if snapshot is None else snapshot.mark_px,
                    "snapshot_ns": None if snapshot is None else snapshot.captured_ns,
                },
                run_id=position.run_id,
                occurred_ns=now_ns,
            ),
        )
        closed.append(position.id)
    session.execute(
        update(models.Order)
        .where(
            models.Order.env == env,
            models.Order.run_id.in_(earlier_runs),
            models.Order.status.not_in(sorted(CLOSED_ORDER_STATUSES)),
        )
        .values(status=OrderStatus.CANCELED, last_event_ns=now_ns, updated_at=func.now())
    )
    return closed


# Decisions, orders, fills, positions ------------------------------------------------------


def record_decision(session: Session, row: DecisionRow) -> None:
    """Record a strategy decision (its id comes from the strategy, before it submits orders)."""
    _check_env(row.env)
    _insert_or_compare(session, models.Decision, column_values(row), ["id"])


def upsert_order(session: Session, row: OrderRow) -> None:
    """Insert or update an order by ``(run_id, client_order_id)``; an older update is ignored.

    ``side``, ``instrument_id``, and ``client_order_id`` never change; ``origin``,
    ``decision_id``, and ``order_type`` change only while the stored order is a stub
    (status ``unknown``). A breach raises ``RecordConflictError``.
    """
    _check_env(row.env)
    _guarded_upsert(
        session,
        models.Order,
        column_values(row),
        key=("run_id", "client_order_id"),
        immutable=_ORDER_IMMUTABLE,
        stub_only=_ORDER_STUB_ONLY,
    )


def _find_order(session: Session, env: str, run_id: UUID, client_order_id: str) -> UUID | None:
    """The order with this client order id in the run, else (paper, live) in an earlier run of the same trader."""
    order = models.Order
    found = session.execute(
        select(order.id).where(order.run_id == run_id, order.client_order_id == client_order_id)
    ).scalar_one_or_none()
    if found is not None:
        return found
    run = session.get(models.StrategyRun, run_id)
    if run is None:
        raise MissingParentError(f"run {run_id} not found")
    if run.mode not in (RunMode.PAPER, RunMode.LIVE):
        return None
    earlier = models.StrategyRun
    return session.execute(
        select(order.id)
        .join(earlier, earlier.id == order.run_id)
        .where(
            order.env == env,
            order.client_order_id == client_order_id,
            earlier.trader_id == run.trader_id,
            earlier.started_at < run.started_at,
        )
        .order_by(earlier.started_at.desc(), order.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _stub_order(session: Session, row: FillRow) -> UUID:
    """Create the stand in order for a fill whose order is unknown, plus an ``orphan_fill`` risk event."""
    stub = OrderRow(
        env=row.env,
        run_id=row.run_id,
        origin=OrderOrigin.EXTERNAL,
        client_order_id=row.client_order_id,
        instrument_id=row.instrument_id,
        side=row.side,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=row.quantity,
        filled_qty=row.quantity,
        status=OrderStatus.UNKNOWN,
        last_event_ns=0,
    )
    session.execute(insert(_table(models.Order)).values(column_values(stub)))
    record_risk_event(
        session,
        RiskEventRow(
            env=row.env,
            event_key=f"orphan_fill:{row.run_id}:{row.trade_id}",
            kind=RiskEventKind.ORPHAN_FILL,
            severity=Severity.WARNING,
            message=f"fill {row.trade_id} arrived for unknown order {row.client_order_id}; stub order created",
            details={"trade_id": row.trade_id, "client_order_id": row.client_order_id},
            run_id=row.run_id,
            order_id=stub.id,
            occurred_ns=row.filled_ns,
        ),
    )
    return stub.id


def record_fill(session: Session, row: FillRow) -> None:
    """Record a fill against the order with its ``client_order_id``.

    When no order can be found, creates a stub order (``origin = 'external'``, ``status =
    'unknown'``) and an ``orphan_fill`` risk event in the same transaction. A fill whose side
    or instrument differs from its order's raises ``RecordConflictError``.
    """
    _check_env(row.env)
    order_id = _find_order(session, row.env, row.run_id, row.client_order_id)
    if order_id is None:
        order_id = _stub_order(session, row)
    else:
        order = models.Order
        side, instrument_id = session.execute(select(order.side, order.instrument_id).where(order.id == order_id)).one()
        if (side, instrument_id) != (row.side, row.instrument_id):
            raise RecordConflictError(
                _table(order).name,
                {"id": order_id, "client_order_id": row.client_order_id},
                {"side": side, "instrument_id": instrument_id},
                {"side": row.side, "instrument_id": row.instrument_id},
            )
    _insert_or_compare(session, models.Fill, column_values(row) | {"order_id": order_id}, ["run_id", "trade_id"])


def upsert_position(session: Session, row: PositionRow) -> None:
    """Insert or update a position by ``(run_id, position_ref, opened_ns)``; an older update is ignored."""
    _check_env(row.env)
    _guarded_upsert(
        session,
        models.Position,
        column_values(row),
        key=("run_id", "position_ref", "opened_ns"),
        immutable=_POSITION_IMMUTABLE,
    )


def record_order_event(session: Session, row: OrderEventRow) -> None:
    """Record an order event against the order with its ``client_order_id`` (found like a fill's).

    Raises ``MissingParentError`` when the order cannot be found.
    """
    _check_env(row.env)
    order_id = _find_order(session, row.env, row.run_id, row.client_order_id)
    if order_id is None:
        raise MissingParentError(f"order {row.client_order_id} not found for run {row.run_id}")
    _insert_or_compare(
        session, models.OrderEvent, column_values(row) | {"order_id": order_id}, ["order_id", "event_id"]
    )


def record_position_snapshot(session: Session, row: PositionSnapshotRow) -> None:
    """Record a position mark to market; raises ``MissingParentError`` for an unknown position."""
    _check_env(row.env)
    if session.get(models.Position, row.position_id) is None:
        raise MissingParentError(f"position {row.position_id} not found")
    _insert_or_compare(session, models.PositionSnapshot, column_values(row), ["position_id", "captured_ns"])


def record_account_snapshot(session: Session, row: AccountSnapshotRow) -> None:
    """Record the balances of one account currency."""
    _check_env(row.env)
    _insert_or_compare(
        session,
        models.AccountSnapshot,
        column_values(row),
        ["env", "run_id", "account_ref", "currency", "captured_ns"],
    )


def record_funding(session: Session, row: FundingRow) -> None:
    """Record a funding payment, keyed by Kraken's log id or, when computed, by run, instrument, and time."""
    _check_env(row.env)
    key, where = _FUNDING_KEYS[row.source]
    _insert_or_compare(session, models.FundingPayment, column_values(row), key, where)


def record_run_result(session: Session, row: RunResultRow) -> None:
    """Record the scored metrics of a run over one sample."""
    _check_env(row.env)
    _insert_or_compare(session, models.RunResult, column_values(row), ["run_id", "sample"])


# Risk events, control commands, and conflicts ---------------------------------------------


def record_risk_event(session: Session, row: RiskEventRow) -> None:
    """Record a risk event, idempotent on ``(env, event_key)``."""
    _check_env(row.env)
    _insert_or_compare(session, models.RiskEvent, column_values(row), ["env", "event_key"])


def report_conflict(session: Session, error: RecordConflictError) -> None:
    """Commit one critical ``record_conflict`` risk event for ``error`` in ``session`` (a fresh one).

    Idempotent: reporting the same conflict again adds no row.
    """
    env = get_settings().trading_env
    key = canonical_json(_json_value(error.key))
    digest = hashlib.sha256(canonical_json(_json_value(error.incoming)).encode()).hexdigest()
    row = RiskEventRow(
        env=Env(env),
        event_key=f"record_conflict:{error.table}:{key}:{digest}",
        kind=RiskEventKind.RECORD_CONFLICT,
        severity=Severity.CRITICAL,
        message=str(error),
        details={
            "table": error.table,
            "key": _json_value(error.key),
            "stored": _json_value(error.stored),
            "incoming": _json_value(error.incoming),
        },
        occurred_ns=time.time_ns(),
    )
    # The report time differs on every call, so only the key decides "already reported".
    session.execute(
        insert(_table(models.RiskEvent))
        .values(column_values(row))
        .on_conflict_do_nothing(index_elements=["env", "event_key"])
    )
    session.commit()


def record_control_command(session: Session, row: ControlCommandRow) -> None:
    """Record a control command as the trader received it, keyed by the message id.

    Receiving the same command again is a no-op, whatever its handling state; a different
    ``cmd``, ``env``, or time for the same id raises ``RecordConflictError``.
    """
    _check_env(row.env)
    _insert_or_compare(session, models.ControlCommand, column_values(row), ["id"], ignore=_CONTROL_HANDLING)


def set_control_outcome(
    session: Session,
    command_id: UUID,
    outcome: ControlOutcome,
    outcome_ns: int,
    details: JsonObject | None = None,
) -> None:
    """Move a ``received`` command to its final outcome, merging ``details`` into the stored ones.

    Setting the same final outcome again is a no-op; any other change raises
    ``InvalidTransitionError``.
    """
    if outcome == ControlOutcome.RECEIVED:
        raise InvalidTransitionError("received is not a final outcome")
    # Locked and reloaded, so a concurrent outcome cannot be lost in the details merge.
    command = session.get(models.ControlCommand, command_id, with_for_update=True, populate_existing=True)
    if command is None:
        raise MissingParentError(f"control command {command_id} not found")
    _check_env(command.env)
    if command.outcome == outcome:
        return
    if command.outcome != ControlOutcome.RECEIVED:
        raise InvalidTransitionError(f"control command {command_id} is already {command.outcome}, not {outcome}")
    session.execute(
        update(models.ControlCommand)
        .where(models.ControlCommand.id == command_id)
        .values(
            outcome=outcome,
            outcome_at=ns_to_utc(outcome_ns),
            details=command.details | normalize_json(details or {}),
            updated_at=func.now(),
        )
    )


# Promotions and run bundles ---------------------------------------------------------------


def record_promotion(session: Session, row: PromotionRow, evidence_run_ids: Sequence[UUID]) -> UUID:
    """Record a promotion of a version with the runs it cites; returns the promotion id.

    Raises ``MissingEvidenceError`` when no run is cited, or a cited run is missing or
    belongs to another strategy version.
    """
    if not evidence_run_ids:
        raise MissingEvidenceError(f"promotion {row.id} cites no evidence run")
    runs = models.StrategyRun
    found = session.execute(select(runs.id, runs.strategy_version_id).where(runs.id.in_(evidence_run_ids)))
    versions = {run_id: version_id for run_id, version_id in found}
    wrong = [run_id for run_id in evidence_run_ids if versions.get(run_id) != row.strategy_version_id]
    if wrong:
        raise MissingEvidenceError(
            f"evidence runs {wrong} are missing or of another version than {row.strategy_version_id}"
        )
    _insert_or_compare(session, models.Promotion, column_values(row), ["id"])
    session.execute(
        insert(_table(models.PromotionEvidence))
        .values([{"promotion_id": row.id, "run_id": run_id} for run_id in evidence_run_ids])
        .on_conflict_do_nothing()
    )
    return row.id


def _bundle_order_ids(bundle: RunBundle, client_order_ids: Sequence[str]) -> dict[str, UUID]:
    by_client_id = {order.client_order_id: order.id for order in bundle.orders}
    missing = sorted(set(client_order_ids) - set(by_client_id))
    if missing:
        raise MissingParentError(f"run bundle has fills or events for orders it does not carry: {missing}")
    return by_client_id


def _check_bundle_rows(bundle: RunBundle) -> None:
    """Every child row must carry the bundle's run id and env, so a sim cannot write into another run."""
    run = bundle.run
    children: list[
        RunResultRow
        | RunFillStatsRow
        | DecisionRow
        | OrderRow
        | OrderEventRow
        | FillRow
        | PositionRow
        | FundingRow
        | RiskEventRow
    ] = [
        *bundle.results,
        *bundle.fill_stats,
        *bundle.decisions,
        *bundle.orders,
        *bundle.order_events,
        *bundle.fills,
        *bundle.positions,
        *bundle.funding,
        *bundle.risk_events,
    ]
    foreign = sorted({(str(row.run_id), str(row.env)) for row in children} - {(str(run.id), str(run.env))})
    if foreign:
        raise ForeignRunError(f"run bundle {run.id} ({run.env}) carries rows of other (run, env) pairs: {foreign}")


def _insert_all(session: Session, model: type[Base], rows: Sequence[dict[str, object]]) -> None:
    if rows:
        session.execute(insert(_table(model)), rows)


def write_run_bundle(session: Session, bundle: RunBundle) -> UUID:
    """Write a finished backtest or replay in one transaction and commit it; returns the run id.

    Raises ``InvalidModeError`` for a paper or live run, and ``ForeignRunError`` when a row
    carries another run id or env. Any error rolls back the whole run. The ``TRADING_ENV``
    check is skipped on purpose (spec 0002 AC-4): a sim run may be tagged with either env.
    """
    run = bundle.run
    if run.mode not in SIM_MODES:
        raise InvalidModeError(f"run bundles are for backtest and replay runs, not {run.mode}")
    _check_bundle_rows(bundle)
    try:
        order_ids = _bundle_order_ids(
            bundle, [e.client_order_id for e in bundle.order_events] + [f.client_order_id for f in bundle.fills]
        )
        _insert_all(session, models.StrategyRun, [column_values(run)])
        _insert_all(session, models.RunResult, [column_values(r) for r in bundle.results])
        _insert_all(session, models.RunFillStats, [column_values(s) for s in bundle.fill_stats])
        _insert_all(session, models.Decision, [column_values(d) for d in bundle.decisions])
        _insert_all(session, models.Order, [column_values(o) for o in bundle.orders])
        # After decisions and orders, which a risk event may reference.
        _insert_all(session, models.RiskEvent, [column_values(r) for r in bundle.risk_events])
        _insert_all(
            session,
            models.OrderEvent,
            [column_values(e) | {"order_id": order_ids[e.client_order_id]} for e in bundle.order_events],
        )
        _insert_all(session, models.Position, [column_values(p) for p in bundle.positions])
        _insert_all(
            session, models.Fill, [column_values(f) | {"order_id": order_ids[f.client_order_id]} for f in bundle.fills]
        )
        _insert_all(session, models.FundingPayment, [column_values(f) for f in bundle.funding])
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return run.id
