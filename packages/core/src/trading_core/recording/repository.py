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
    Env,
    OrderOrigin,
    OrderStatus,
    OrderType,
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
    InvalidTransitionError,
    MissingParentError,
    PromotionRequiredError,
    RecordConflictError,
    UnknownCommitError,
)
from trading_core.recording.rows import (
    DecisionRow,
    FillRow,
    InstrumentRow,
    JsonObject,
    JsonValue,
    OrderRow,
    PositionRow,
    RiskEventRow,
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
    clauses = [table.c[name].is_not_distinct_from(value) for name, value in key.items()]
    if where is None:
        return and_(*clauses)
    return and_(*clauses, text(where))


def _stored(
    session: Session, table: Table, key: Mapping[str, object], where: str | None = None
) -> dict[str, object] | None:
    row = session.execute(select(table).where(_key_filter(table, key, where))).mappings().one_or_none()
    return None if row is None else {str(name): value for name, value in row.items()}


def _differences(stored: Mapping[str, object], incoming: Mapping[str, object]) -> list[str]:
    stored_content, incoming_content = _content(stored), _content(incoming)
    differing = []
    for name, value in incoming_content.items():
        kept = stored_content.get(name)
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
) -> bool:
    """INSERT … ON CONFLICT (key) DO NOTHING; on a conflict compare with the stored row.

    Returns True when a row was inserted. Equal content is a no-op (a stored NULL
    ``position_id`` is then set from the incoming value); different content raises
    ``RecordConflictError``. ``where`` names the partial unique index to arbitrate on.
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
    if _differences(stored, values):
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
    """
    table = _table(model)
    key_values = {name: values[name] for name in key}
    stored = _stored(session, table, key_values)
    if stored is not None:
        frozen = list(immutable)
        if stored["status"] != OrderStatus.UNKNOWN:
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
    session: Session, name: str, kind: StrategyKind, git_commit: str, config: JsonObject
) -> UUID:
    """Return the id of the version with this commit and config, creating strategy and version if new.

    Raises ``RecordConflictError`` when the strategy name exists with another kind, or the
    version hash already belongs to another strategy.
    """
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
    'unknown'``) and an ``orphan_fill`` risk event in the same transaction.
    """
    _check_env(row.env)
    order_id = _find_order(session, row.env, row.run_id, row.client_order_id)
    if order_id is None:
        order_id = _stub_order(session, row)
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


# Risk events and conflicts ----------------------------------------------------------------


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
