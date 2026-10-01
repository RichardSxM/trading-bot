"""Frozen row values the write API accepts, one per table (spec 0002).

Field names match the column names, so ``column_values`` maps a row straight onto an
INSERT. Ids default to ``uuid.uuid7()``. Every ``<name>_at`` field with a ``<name>_ns``
sibling is derived from it (microsecond precision), never passed in. Money and sizes are
``Decimal``; a row refuses a float, a non finite ``Decimal``, or a naive datetime. JSON
fields are normalized on the way in (``Decimal`` to string, sorted keys) so a stored JSONB
value compares equal to the row that wrote it.
"""

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from trading_core.recording.enums import (
    ControlCmd,
    ControlOutcome,
    DecidedBy,
    Env,
    FundingSource,
    Granularity,
    InstrumentKind,
    Intent,
    Liquidity,
    OrderEventKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RiskVerdict,
    RunMode,
    RunStatus,
    Sample,
    Severity,
    Stage,
    TimeInForce,
    Verdict,
)

type JsonValue = str | int | bool | Decimal | Sequence[JsonValue] | Mapping[str, JsonValue] | None
type JsonObject = Mapping[str, JsonValue]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NOT_A_COLUMN: Final = {"column": False}


def ns_to_utc(ns: int) -> datetime:
    """UTC datetime for a Nautilus nanosecond timestamp, truncated to microseconds."""
    return _EPOCH + timedelta(microseconds=ns // 1000)


def _normalize(value: object) -> object:
    if isinstance(value, bool | str | int) or value is None:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non finite Decimal {value!r} in JSON")
        return format(value, "f")  # fixed notation: str() would give "1E-8"
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    raise TypeError(f"{type(value).__name__} is not allowed in JSON (use Decimal for numbers with a fraction)")


def canonical_json(value: JsonValue) -> str:
    """JSON with sorted keys, no spaces, and ``Decimal`` as a string; refuses floats."""
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"))


def normalize_json(value: JsonObject) -> dict[str, JsonValue]:
    """The plain dict Postgres hands back after storing ``value`` as JSONB."""
    normalized: dict[str, JsonValue] = json.loads(canonical_json(value))
    return normalized


def version_hash(git_commit: str, config: JsonObject) -> str:
    """sha256 hex of the canonical JSON of ``{git_commit, config}``: the strategy version key."""
    payload = canonical_json({"git_commit": git_commit, "config": config})
    return hashlib.sha256(payload.encode()).hexdigest()


class _Row:
    """Validation and ``_at`` derivation shared by every row dataclass."""

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):  # type: ignore[arg-type]  # subclasses are dataclasses
            if not f.init and f.name.endswith("_at"):
                ns = getattr(self, f.name.removesuffix("_at") + "_ns")
                object.__setattr__(self, f.name, None if ns is None else ns_to_utc(ns))
            value = getattr(self, f.name)
            if isinstance(value, float):
                raise TypeError(f"{type(self).__name__}.{f.name} is a float; use Decimal")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError(f"{type(self).__name__}.{f.name} is not finite: {value!r}")
            if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
                raise ValueError(f"{type(self).__name__}.{f.name} must be a UTC aware datetime")
            if isinstance(value, Mapping):
                object.__setattr__(self, f.name, normalize_json(value))


def column_values(row: _Row) -> dict[str, object]:
    """The row as ``{column: value}``, leaving out lookup only fields (``client_order_id`` on fills)."""
    return {
        f.name: getattr(row, f.name)
        for f in dataclasses.fields(row)  # type: ignore[arg-type]  # every _Row is a dataclass
        if f.metadata.get("column", True)
    }


@dataclass(frozen=True, kw_only=True)
class InstrumentRow(_Row):
    """Instrument specs from the Nautilus instrument provider (``instruments``)."""

    instrument_id: str
    venue: str
    symbol: str
    kind: InstrumentKind = InstrumentKind.PERPETUAL
    base_currency: str
    quote_currency: str
    settle_currency: str
    tick_size: Decimal
    lot_size: Decimal
    multiplier: Decimal
    is_inverse: bool


@dataclass(frozen=True, kw_only=True)
class RunRow(_Row):
    """A strategy run (``strategy_runs``); built by ``start_run`` or carried in a ``RunBundle``."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    mode: RunMode
    status: RunStatus = RunStatus.RUNNING
    strategy_version_id: UUID
    trader_id: str
    run_config: JsonObject
    data_start_at: datetime | None = None
    data_end_at: datetime | None = None
    started_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class RunResultRow(_Row):
    """Scored metrics of a run over one sample (``run_results``); fractions, not percents."""

    run_id: UUID
    env: Env
    sample: Sample
    return_after_costs: Decimal
    max_drawdown: Decimal
    risk_adjusted_ratio: Decimal | None = None
    ratio_kind: str | None = None
    win_rate: Decimal
    trade_count: int
    fees_total: Decimal
    funding_total: Decimal
    currency: str
    computed_at: datetime


@dataclass(frozen=True, kw_only=True)
class DecisionRow(_Row):
    """A strategy decision and its risk verdict (``decisions``); the strategy sets ``id``."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    instrument_id: str
    intent: Intent
    side: OrderSide | None = None
    reason: str
    inputs: JsonObject
    risk_verdict: RiskVerdict
    risk_reason: str | None = None
    decided_ns: int
    decided_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class OrderRow(_Row):
    """Latest known state of an order (``orders``), guarded by ``last_event_ns``."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    decision_id: UUID | None = None
    origin: OrderOrigin
    client_order_id: str
    venue_order_id: str | None = None
    parent_client_order_id: str | None = None
    order_list_id: str | None = None
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    reduce_only: bool = False
    post_only: bool = False
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    avg_px: Decimal | None = None
    last_event_ns: int


@dataclass(frozen=True, kw_only=True)
class OrderEventRow(_Row):
    """One Nautilus order event (``order_events``); found by ``client_order_id`` in ``run_id``."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID = field(metadata=_NOT_A_COLUMN)
    client_order_id: str = field(metadata=_NOT_A_COLUMN)
    event_id: UUID
    kind: OrderEventKind
    details: JsonObject
    occurred_ns: int
    occurred_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class FillRow(_Row):
    """One execution (``fills``); found by ``client_order_id``, never by order id."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    client_order_id: str = field(metadata=_NOT_A_COLUMN)
    position_id: UUID | None = None
    instrument_id: str
    trade_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    liquidity: Liquidity
    filled_ns: int
    filled_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class PositionRow(_Row):
    """Latest known state of a position (``positions``); ``quantity`` is signed."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    instrument_id: str
    position_ref: str
    side: PositionSide
    status: PositionStatus = PositionStatus.OPEN
    quantity: Decimal
    peak_qty: Decimal
    avg_entry_px: Decimal
    avg_exit_px: Decimal | None = None
    realized_pnl: Decimal
    fees_total: Decimal
    funding_total: Decimal
    currency: str
    opened_ns: int
    opened_at: datetime = field(init=False)
    closed_at: datetime | None = None
    last_event_ns: int


@dataclass(frozen=True, kw_only=True)
class PositionSnapshotRow(_Row):
    """Mark to market of one position (``position_snapshots``)."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    position_id: UUID
    quantity: Decimal
    mark_px: Decimal
    unrealized_pnl: Decimal
    liquidation_px: Decimal | None = None
    currency: str
    granularity: Granularity = Granularity.RAW
    captured_ns: int
    captured_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class AccountSnapshotRow(_Row):
    """Balances of one account currency (``account_snapshots``)."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID | None = None
    account_ref: str
    balance_total: Decimal
    balance_free: Decimal
    margin_used: Decimal
    currency: str
    granularity: Granularity = Granularity.RAW
    captured_ns: int
    captured_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class FundingRow(_Row):
    """One funding payment (``funding_payments``); ``amount`` is negative when paid."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    position_id: UUID | None = None
    instrument_id: str
    amount: Decimal
    currency: str
    rate: Decimal | None = None
    source: FundingSource
    external_id: str | None = None
    paid_ns: int
    paid_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class RiskEventRow(_Row):
    """Something the risk layer or the recorder reports (``risk_events``), keyed by ``event_key``."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    event_key: str
    kind: RiskEventKind
    severity: Severity
    message: str
    details: JsonObject = field(default_factory=dict)
    run_id: UUID | None = None
    decision_id: UUID | None = None
    order_id: UUID | None = None
    occurred_ns: int
    occurred_at: datetime = field(init=False)


@dataclass(frozen=True, kw_only=True)
class ControlCommandRow(_Row):
    """A dashboard command as the trader received it (``control_commands``); ``id`` is the message id."""

    id: UUID
    env: Env
    cmd: ControlCmd
    issued_at: datetime
    expires_at: datetime
    received_at: datetime
    outcome: ControlOutcome = ControlOutcome.RECEIVED
    outcome_at: datetime | None = None
    details: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class PromotionRow(_Row):
    """A promotion decision for one strategy version (``promotions``)."""

    id: UUID = field(default_factory=uuid.uuid7)
    strategy_version_id: UUID
    from_stage: Stage
    to_stage: Stage
    verdict: Verdict
    thresholds: JsonObject
    decided_by: DecidedBy
    notes: str | None = None
    decided_at: datetime


@dataclass(frozen=True, kw_only=True)
class FxRateRow(_Row):
    """One ECB daily reference rate (``fx_rates``): ``rate`` quote units per 1 base unit."""

    rate_date: date
    base_currency: str = "EUR"
    quote_currency: str = "USD"
    rate: Decimal
    source: str = "ECB"


@dataclass(frozen=True, kw_only=True)
class RunBundle:
    """A finished backtest or replay, written in one transaction by ``write_run_bundle``."""

    run: RunRow
    results: Sequence[RunResultRow] = ()
    decisions: Sequence[DecisionRow] = ()
    orders: Sequence[OrderRow] = ()
    order_events: Sequence[OrderEventRow] = ()
    fills: Sequence[FillRow] = ()
    positions: Sequence[PositionRow] = ()
    funding: Sequence[FundingRow] = ()
