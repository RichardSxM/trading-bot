"""The 18 Postgres tables of spec 0002, ``run_fill_stats`` (spec 0008), ``alerts`` (spec 0013), the
gate's ``studies``, ``study_runs``, ``run_daily_equity``, ``run_measures``, and ``promotion_folds`` (spec 0015),
and ``reconciliations`` with ``reconciliation_mismatches`` (spec 0023).

All live on ``trading_core.db.Base``.

Market data never lands here (it stays in the Parquet catalog). Env scoped tables link to
their parents with a composite ``(<parent>_id, env)`` foreign key against the parent's
``UNIQUE (id, env)``, so a row can never point at a parent in the other environment. Enums
are ``text`` with a named CHECK built from ``trading_core.recording.enums``.
"""

import uuid
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    PrimaryKeyConstraint,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_core.alerts.model import Delivery, Tier
from trading_core.db import Base
from trading_core.recording.enums import (
    CLOSED_ORDER_STATUSES,
    TERMINAL_RUN_STATUSES,
    ActionResult,
    ControlCmd,
    ControlOutcome,
    DecidedBy,
    Env,
    EquityBasis,
    FundingSource,
    Granularity,
    InstrumentKind,
    Intent,
    Liquidity,
    MismatchAction,
    MismatchKind,
    OrderEventKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    ReconcileOutcome,
    ReconcileTrigger,
    RiskEventKind,
    RiskVerdict,
    RunMode,
    RunStatus,
    Sample,
    Severity,
    Stage,
    StrategyKind,
    StudyLab,
    StudyRole,
    TimeInForce,
    Verdict,
)


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _one_of(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """Named CHECK ``<column> IN (...)`` listing every member of ``enum``."""
    return CheckConstraint(f"{column} IN ({_quoted(enum)})", name=f"{column}_valid")


def _env_fk(column: str, parent: str) -> ForeignKeyConstraint:
    """Composite FK ``(<column>, env)`` to ``<parent>(id, env)``: the parent must share the env."""
    return ForeignKeyConstraint([column, "env"], [f"{parent}.id", f"{parent}.env"])


def _ix(table: str, *columns: str, where: str | None = None, unique: bool = False) -> Index:
    """Index named like the convention (``ix_`` or ``uq_``, table, every column)."""
    name = f"{'uq' if unique else 'ix'}_{table}_{'_'.join(columns)}"
    if where is None:
        return Index(name, *columns, unique=unique)
    return Index(name, *columns, unique=unique, postgresql_where=text(where))


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


def _env_check() -> CheckConstraint:
    return _one_of("env", Env)


# Global tables (no env) -------------------------------------------------------------------


class Instrument(Base):
    """Instrument specs from the Nautilus instrument provider; a changed spec is a conflict."""

    __tablename__ = "instruments"
    __table_args__ = (_one_of("kind", InstrumentKind),)

    instrument_id: Mapped[str] = mapped_column(primary_key=True)
    venue: Mapped[str]
    symbol: Mapped[str]
    kind: Mapped[str]
    base_currency: Mapped[str]
    quote_currency: Mapped[str]
    settle_currency: Mapped[str]
    tick_size: Mapped[Decimal]
    lot_size: Mapped[Decimal]
    multiplier: Mapped[Decimal]
    is_inverse: Mapped[bool]
    created_at: Mapped[datetime] = _created()


class Strategy(Base):
    """A named strategy; its code and config live in ``strategy_versions``."""

    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("name"), _one_of("kind", StrategyKind))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    kind: Mapped[str]
    description: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class StrategyVersion(Base):
    """One exact strategy: commit plus config, keyed by ``version_hash``."""

    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("version_hash"), _ix("strategy_versions", "frozen_hash"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    git_commit: Mapped[str]
    config: Mapped[dict[str, object]]
    version_hash: Mapped[str]
    # Spec 0015 AC-1: the strategy's own code closure, and that plus config and Nautilus version.
    # NULL on rows from before migration 0009.
    code_hash: Mapped[str | None]
    frozen_hash: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class Promotion(Base):
    """A promotion decision for a version between two stages; append only (spec 0015)."""

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint(f"from_stage IN ({_quoted(Stage)})", name="from_stage_valid"),
        CheckConstraint(f"to_stage IN ({_quoted(Stage)})", name="to_stage_valid"),
        _one_of("verdict", Verdict),
        _one_of("decided_by", DecidedBy),
        CheckConstraint("decided_by = 'gate' OR to_stage <> 'live'", name="no_manual_live"),
        CheckConstraint("decided_by = 'manual' OR frozen_hash IS NOT NULL", name="gate_has_frozen_hash"),
        _ix("promotions", "frozen_hash", "to_stage", "decided_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    from_stage: Mapped[str]
    to_stage: Mapped[str]
    verdict: Mapped[str]
    thresholds: Mapped[dict[str, object]]
    decided_by: Mapped[str]
    notes: Mapped[str | None]
    decided_at: Mapped[datetime]
    frozen_hash: Mapped[str | None]
    study_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("studies.id"))
    thresholds_hash: Mapped[str | None]
    reasons: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    metrics: Mapped[dict[str, object]] = mapped_column(server_default=text("'{}'::jsonb"))
    gate_commit: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class Study(Base):
    """One strategy tested under a plan written before its result is seen (spec 0015 AC-2); immutable."""

    __tablename__ = "studies"
    __table_args__ = (UniqueConstraint("name"), _one_of("lab", StudyLab))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id"), index=True)
    name: Mapped[str]
    lab: Mapped[str]
    code_hash: Mapped[str]
    plan: Mapped[dict[str, object]]
    plan_hash: Mapped[str]
    thresholds: Mapped[dict[str, object]]
    thresholds_hash: Mapped[str]
    created_at: Mapped[datetime] = _created()


class StudyRun(Base):
    """A run's role in its study (spec 0015 AC-3, AC-5); a failed run counts as a trial too. Immutable."""

    __tablename__ = "study_runs"
    __table_args__ = (
        PrimaryKeyConstraint("study_id", "run_id"),
        UniqueConstraint("run_id"),
        _one_of("role", StudyRole),
        CheckConstraint(f"status IN ({_quoted(TERMINAL_RUN_STATUSES)})", name="status_valid"),
        CheckConstraint("(role = 'benchmark') = (grid_point IS NULL)", name="grid_point_iff_not_benchmark"),
        # At most one completed holdout per frozen version, in any study (AC-5).
        _ix("study_runs", "frozen_hash", where="role = 'holdout' AND status = 'completed'", unique=True),
        # And at most one per study, whatever its grid points' hashes (migration 0010).
        _ix("study_runs", "study_id", where="role = 'holdout' AND status = 'completed'", unique=True),
    )

    study_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("studies.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_runs.id"))
    role: Mapped[str]
    grid_point: Mapped[int | None]
    frozen_hash: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime] = _created()


class PromotionFold(Base):
    """One walk forward fold an evaluation scored (spec 0015 AC-9); immutable."""

    __tablename__ = "promotion_folds"
    __table_args__ = (PrimaryKeyConstraint("promotion_id", "fold"),)

    promotion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("promotions.id"))
    fold: Mapped[int]
    is_start: Mapped[date]
    is_end: Mapped[date]
    oos_start: Mapped[date]
    oos_end: Mapped[date]
    chosen_grid_point: Mapped[int]
    is_sharpe: Mapped[Decimal | None]
    oos_sharpe: Mapped[Decimal | None]
    oos_return: Mapped[Decimal]
    oos_trades: Mapped[int]
    oos_net_pnl: Mapped[Decimal]
    created_at: Mapped[datetime] = _created()


class PromotionEvidence(Base):
    """The runs a promotion cites as evidence."""

    __tablename__ = "promotion_evidence"
    __table_args__ = (PrimaryKeyConstraint("promotion_id", "run_id"),)

    promotion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("promotions.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_runs.id"), index=True)
    created_at: Mapped[datetime] = _created()


class FxRate(Base):
    """ECB daily reference rate: ``rate`` quote units (USD) per 1 base unit (EUR)."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        PrimaryKeyConstraint("rate_date", "base_currency", "quote_currency"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    rate_date: Mapped[date]
    base_currency: Mapped[str]
    quote_currency: Mapped[str]
    rate: Mapped[Decimal]
    source: Mapped[str]
    created_at: Mapped[datetime] = _created()


# Env scoped tables ------------------------------------------------------------------------


class StrategyRun(Base):
    """One run of a strategy version: a backtest, a replay, or one paper or live process start."""

    __tablename__ = "strategy_runs"
    __table_args__ = (
        UniqueConstraint("id", "env"),
        _env_check(),
        _one_of("mode", RunMode),
        _one_of("status", RunStatus),
        CheckConstraint("mode <> 'live' OR env = 'live'", name="live_mode_in_live_env"),
        CheckConstraint("(status = 'running') = (ended_at IS NULL)", name="ended_at_iff_finished"),
        _ix("strategy_runs", "strategy_version_id", "mode"),
        _ix("strategy_runs", "env", "trader_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    mode: Mapped[str]
    status: Mapped[str]
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_versions.id"))
    trader_id: Mapped[str]
    run_config: Mapped[dict[str, object]]
    data_start_at: Mapped[datetime | None]
    data_end_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class RunResult(Base):
    """Scored metrics of a run over one sample window."""

    __tablename__ = "run_results"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "sample"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("sample", Sample),
    )

    run_id: Mapped[uuid.UUID]
    env: Mapped[str]
    sample: Mapped[str]
    return_after_costs: Mapped[Decimal]
    max_drawdown: Mapped[Decimal]
    risk_adjusted_ratio: Mapped[Decimal | None]
    ratio_kind: Mapped[str | None]
    win_rate: Mapped[Decimal]
    trade_count: Mapped[int]
    fees_total: Mapped[Decimal]
    funding_total: Mapped[Decimal]
    currency: Mapped[str]
    computed_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()


class RunFillStats(Base):
    """Fill rate and markouts of a replay's entry orders, one row per liquidity side (spec 0008 AC-10)."""

    __tablename__ = "run_fill_stats"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "liquidity"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        # Only the two sides an order can fill on; no_liquidity never gets a row.
        CheckConstraint("liquidity IN ('maker', 'taker')", name="liquidity_valid"),
        CheckConstraint("orders_placed >= 1", name="orders_placed_positive"),
        CheckConstraint("orders_filled BETWEEN 0 AND orders_placed", name="orders_filled_range"),
        CheckConstraint("fill_rate BETWEEN 0 AND 1", name="fill_rate_range"),
        CheckConstraint("fill_count >= 0", name="fill_count_nonnegative"),
    )

    run_id: Mapped[uuid.UUID]
    env: Mapped[str]
    liquidity: Mapped[str]
    orders_placed: Mapped[int]
    orders_filled: Mapped[int]
    fill_rate: Mapped[Decimal]
    fill_count: Mapped[int]
    markout_1s_bps: Mapped[Decimal | None]
    markout_5s_bps: Mapped[Decimal | None]
    markout_30s_bps: Mapped[Decimal | None]
    computed_at: Mapped[datetime]


class RunDailyEquity(Base):
    """Equity at each UTC day's close of a backtest or replay run (spec 0015 AC-6); immutable."""

    __tablename__ = "run_daily_equity"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "day"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("basis", EquityBasis),
        CheckConstraint("trades_closed >= 0", name="trades_closed_nonnegative"),
    )

    run_id: Mapped[uuid.UUID]
    env: Mapped[str]
    day: Mapped[date]
    equity_close: Mapped[Decimal]
    basis: Mapped[str]
    trades_closed: Mapped[int]
    fees: Mapped[Decimal]
    funding: Mapped[Decimal]
    currency: Mapped[str]


class RunMeasures(Base):
    """A run's drawdown, splits, concentration, and cost per trade (spec 0015); immutable.

    ``avg_gross_edge`` and ``avg_round_trip_cost`` are NULL for a run with no closed trade.
    """

    __tablename__ = "run_measures"
    __table_args__ = (
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        CheckConstraint(f"drawdown_basis IN ({_quoted(EquityBasis)})", name="drawdown_basis_valid"),
        CheckConstraint("long_trades >= 0 AND short_trades >= 0", name="trades_nonnegative"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    drawdown_basis: Mapped[str]
    marked_max_drawdown: Mapped[Decimal | None]
    longest_drawdown_hours: Mapped[int | None]
    exposure: Mapped[Decimal | None]
    long_trades: Mapped[int]
    long_net_pnl: Mapped[Decimal]
    short_trades: Mapped[int]
    short_net_pnl: Mapped[Decimal]
    profit_factor: Mapped[Decimal | None]
    expectancy: Mapped[Decimal | None]
    top_trades_profit_share: Mapped[Decimal | None]
    avg_gross_edge: Mapped[Decimal | None]
    avg_round_trip_cost: Mapped[Decimal | None]
    currency: Mapped[str]


class Decision(Base):
    """A strategy decision with its inputs and the risk check's verdict."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("id", "env"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("intent", Intent),
        _one_of("side", OrderSide),
        _one_of("risk_verdict", RiskVerdict),
        _ix("decisions", "run_id", "decided_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID]
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    intent: Mapped[str]
    side: Mapped[str | None]
    reason: Mapped[str]
    inputs: Mapped[dict[str, object]]
    risk_verdict: Mapped[str]
    risk_reason: Mapped[str | None]
    decided_at: Mapped[datetime]
    decided_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class Order(Base):
    """Latest known state of an order; ``last_event_ns`` keeps it from moving backwards."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("run_id", "client_order_id"),
        UniqueConstraint("id", "env"),
        _env_fk("run_id", "strategy_runs"),
        _env_fk("decision_id", "decisions"),
        _env_check(),
        _one_of("origin", OrderOrigin),
        _one_of("side", OrderSide),
        _one_of("order_type", OrderType),
        _one_of("time_in_force", TimeInForce),
        _one_of("status", OrderStatus),
        CheckConstraint("origin <> 'strategy' OR decision_id IS NOT NULL", name="strategy_order_has_decision"),
        _ix("orders", "env", "status", where=f"status NOT IN ({_quoted(sorted(CLOSED_ORDER_STATUSES))})"),
        _ix("orders", "env", "client_order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID]
    decision_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    origin: Mapped[str]
    client_order_id: Mapped[str]
    venue_order_id: Mapped[str | None]
    parent_client_order_id: Mapped[str | None]
    order_list_id: Mapped[str | None]
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    side: Mapped[str]
    order_type: Mapped[str]
    time_in_force: Mapped[str]
    quantity: Mapped[Decimal]
    price: Mapped[Decimal | None]
    trigger_price: Mapped[Decimal | None]
    reduce_only: Mapped[bool]
    post_only: Mapped[bool]
    status: Mapped[str]
    filled_qty: Mapped[Decimal]
    avg_px: Mapped[Decimal | None]
    last_event_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class OrderEvent(Base):
    """One Nautilus order event, append only."""

    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "event_id"),
        _env_fk("order_id", "orders"),
        _env_check(),
        _one_of("kind", OrderEventKind),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    order_id: Mapped[uuid.UUID]
    event_id: Mapped[uuid.UUID]
    kind: Mapped[str]
    details: Mapped[dict[str, object]]
    occurred_at: Mapped[datetime]
    occurred_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class Position(Base):
    """Latest known state of a position; a reopened NETTING position is a new row."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("run_id", "position_ref", "opened_ns"),
        UniqueConstraint("id", "env"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("side", PositionSide),
        _one_of("status", PositionStatus),
        _ix("positions", "env", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID]
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    position_ref: Mapped[str]
    side: Mapped[str]
    status: Mapped[str]
    quantity: Mapped[Decimal]
    peak_qty: Mapped[Decimal]
    avg_entry_px: Mapped[Decimal]
    avg_exit_px: Mapped[Decimal | None]
    realized_pnl: Mapped[Decimal]
    fees_total: Mapped[Decimal]
    funding_total: Mapped[Decimal]
    currency: Mapped[str]
    opened_at: Mapped[datetime]
    opened_ns: Mapped[int] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None]
    last_event_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Fill(Base):
    """One execution, append only (``position_id`` may be set once from NULL)."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("run_id", "trade_id"),
        _env_fk("run_id", "strategy_runs"),
        _env_fk("order_id", "orders"),
        _env_fk("position_id", "positions"),
        _env_check(),
        _one_of("side", OrderSide),
        _one_of("liquidity", Liquidity),
        _ix("fills", "env", "filled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID]
    order_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    trade_id: Mapped[str]
    side: Mapped[str]
    quantity: Mapped[Decimal]
    price: Mapped[Decimal]
    fee: Mapped[Decimal]
    fee_currency: Mapped[str]
    liquidity: Mapped[str]
    filled_at: Mapped[datetime]
    filled_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class PositionSnapshot(Base):
    """Mark to market of one position; thinned to hourly after 90 days."""

    __tablename__ = "position_snapshots"
    __table_args__ = (
        UniqueConstraint("position_id", "captured_ns"),
        _env_fk("position_id", "positions"),
        _env_check(),
        _one_of("granularity", Granularity),
        _ix("position_snapshots", "position_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    position_id: Mapped[uuid.UUID]
    quantity: Mapped[Decimal]
    mark_px: Mapped[Decimal]
    unrealized_pnl: Mapped[Decimal]
    liquidation_px: Mapped[Decimal | None]
    currency: Mapped[str]
    granularity: Mapped[str]
    captured_at: Mapped[datetime]
    captured_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class AccountSnapshot(Base):
    """Balances of one account currency; thinned to hourly after 90 days."""

    __tablename__ = "account_snapshots"
    __table_args__ = (
        # Named by hand: the convention's name is 64 characters, one past the Postgres limit.
        UniqueConstraint(
            "env",
            "run_id",
            "account_ref",
            "currency",
            "captured_ns",
            name="uq_account_snapshots_series_captured_ns",
            postgresql_nulls_not_distinct=True,
        ),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("granularity", Granularity),
        _ix("account_snapshots", "env", "account_ref", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    account_ref: Mapped[str]
    balance_total: Mapped[Decimal]
    balance_free: Mapped[Decimal]
    margin_used: Mapped[Decimal]
    currency: Mapped[str]
    granularity: Mapped[str]
    captured_at: Mapped[datetime]
    captured_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class FundingPayment(Base):
    """One funding payment, from Kraken's account log or computed by the simulator."""

    __tablename__ = "funding_payments"
    __table_args__ = (
        _env_fk("run_id", "strategy_runs"),
        _env_fk("position_id", "positions"),
        _env_check(),
        _one_of("source", FundingSource),
        CheckConstraint("source <> 'kraken_log' OR external_id IS NOT NULL", name="kraken_log_has_external_id"),
        _ix("funding_payments", "env", "external_id", where="source = 'kraken_log'", unique=True),
        _ix("funding_payments", "run_id", "instrument_id", "paid_ns", where="source = 'computed'", unique=True),
        _ix("funding_payments", "env", "paid_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    amount: Mapped[Decimal]
    currency: Mapped[str]
    rate: Mapped[Decimal | None]
    source: Mapped[str]
    external_id: Mapped[str | None]
    paid_at: Mapped[datetime]
    paid_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class RiskEvent(Base):
    """Something the risk layer or the recorder reports, keyed by a deterministic ``event_key``."""

    __tablename__ = "risk_events"
    __table_args__ = (
        UniqueConstraint("env", "event_key"),
        _env_fk("run_id", "strategy_runs"),
        _env_fk("decision_id", "decisions"),
        _env_fk("order_id", "orders"),
        _env_check(),
        _one_of("kind", RiskEventKind),
        _one_of("severity", Severity),
        _ix("risk_events", "env", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    event_key: Mapped[str]
    kind: Mapped[str]
    severity: Mapped[str]
    message: Mapped[str]
    details: Mapped[dict[str, object]]
    run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    occurred_at: Mapped[datetime]
    occurred_ns: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()


class ControlCommand(Base):
    """A dashboard command the trader read from the ``control`` stream, keyed by the message id."""

    __tablename__ = "control_commands"
    __table_args__ = (
        _env_check(),
        _one_of("cmd", ControlCmd),
        _one_of("outcome", ControlOutcome),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    cmd: Mapped[str]
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    received_at: Mapped[datetime]
    outcome: Mapped[str]
    outcome_at: Mapped[datetime | None]
    details: Mapped[dict[str, object]]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# Reconciliation (spec 0023) -----------------------------------------------------------------


class Reconciliation(Base):
    """One check of the trader's orders and positions against the exchange's (spec 0023 AC-17)."""

    __tablename__ = "reconciliations"
    __table_args__ = (
        UniqueConstraint("id", "env"),
        _env_fk("run_id", "strategy_runs"),
        _env_check(),
        _one_of("trigger", ReconcileTrigger),
        _one_of("outcome", ReconcileOutcome),
        _ix("reconciliations", "env", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    run_id: Mapped[uuid.UUID]
    trigger: Mapped[str]
    started_at: Mapped[datetime]
    started_ns: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[datetime | None]
    finished_ns: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str | None]
    exchange_orders: Mapped[int | None]
    exchange_positions: Mapped[int | None]
    mismatch_count: Mapped[int] = mapped_column(server_default=text("0"))
    error: Mapped[str | None]
    created_at: Mapped[datetime] = _created()


class ReconciliationMismatch(Base):
    """One finding of a reconciliation check, with the action taken and how it went (spec 0023 AC-13)."""

    __tablename__ = "reconciliation_mismatches"
    __table_args__ = (
        _env_fk("reconciliation_id", "reconciliations"),
        _env_fk("order_id", "orders"),
        _env_fk("position_id", "positions"),
        _env_check(),
        _one_of("kind", MismatchKind),
        _one_of("action", MismatchAction),
        _one_of("action_result", ActionResult),
        _ix("reconciliation_mismatches", "env", "reconciliation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    env: Mapped[str]
    reconciliation_id: Mapped[uuid.UUID]
    kind: Mapped[str]
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    client_order_id: Mapped[str | None]
    venue_order_id: Mapped[str | None]
    order_id: Mapped[uuid.UUID | None]
    position_id: Mapped[uuid.UUID | None]
    exchange_side: Mapped[str | None]
    exchange_qty: Mapped[Decimal | None]
    local_side: Mapped[str | None]
    local_qty: Mapped[Decimal | None]
    action: Mapped[str]
    action_result: Mapped[str]
    details: Mapped[dict[str, object]]
    created_at: Mapped[datetime] = _created()


# Alerts (spec 0013) -------------------------------------------------------------------------


class Alert(Base):
    """One alert episode: opened, repeated, pushed, resolved. Kept forever; written only by the alerter.

    ``uq_alerts_open`` allows one open episode per ``(env, fingerprint)``; a resolved one frees the
    fingerprint for the next episode.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        _env_check(),
        _one_of("severity", Tier),
        _one_of("delivery", Delivery),
        CheckConstraint("occurrences >= 1", name="occurrences_positive"),
        CheckConstraint("sends >= 0", name="sends_nonnegative"),
        Index("uq_alerts_open", "env", "fingerprint", unique=True, postgresql_where=text("resolved_at IS NULL")),
        _ix("alerts", "env", "opened_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    env: Mapped[str]
    rule: Mapped[str]
    fingerprint: Mapped[str]
    severity: Mapped[str]
    source: Mapped[str]
    title: Mapped[str]
    message: Mapped[str]
    details: Mapped[dict[str, object]]
    opened_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    occurrences: Mapped[int] = mapped_column(server_default=text("1"))
    sends: Mapped[int] = mapped_column(server_default=text("0"))
    last_sent_at: Mapped[datetime | None]
    delivery: Mapped[str]
