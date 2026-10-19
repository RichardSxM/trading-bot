"""``lab replay``: plan the segments, run each variant's units, and write one bundle and report per variant.

Order (spec 0008 AC-3, AC-11, AC-13): the run file is already valid; the required days must all
have manifests (else exit 2, nothing written); the instrument and strategy version rows are
ensured; then every variant runs its units in plan order, and the parent merges their rows,
stamps the run id, scores them, and writes the bundle in one transaction. A variant that fails
writes no row, only a ``failed`` report. ``index.md`` is always written.

Variants (AC-6) share one ``batch_id``. Their units run in spawned workers (``lab.pool``, AC-16),
started in plan order, ``base`` first. A variant is written (bundle, then report) only once every
variant before it is written, so bundles and reports keep the variant order whatever order the
units finish in. A unit that fails fails only its variant (AC-13). SIGINT or SIGTERM stops every
unit; variants not complete get an ``interrupted`` report and no bundle, and the exit is 130.
"""

import logging
import signal
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Final, Literal, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from lab.actors import SEGMENT_END
from lab.catalog import SourceTree
from lab.instruments import pinned_instrument
from lab.pool import Target, UnitDone, UnitPool
from lab.report import (
    BandReport,
    ErrorInfo,
    FillStatsReport,
    LabReport,
    Results,
    Status,
    index_markdown,
    write_variant,
)
from lab.runfile import LoadedRun
from lab.settings import LabSettings
from lab.strategies import ConfigValue, registry_entry
from lab.study_rows import RATIO_KIND, daily_equity_rows, daily_sharpe, measures_row
from lab.units import TRADER_ID, UnitPlan, UnitResult, run_unit
from trading_core.lab.daily import ClosedPosition, day_of, realized_daily, trade_measures
from trading_core.lab.errors import ReplayInterruptedError, UnitFailedError
from trading_core.lab.fill_stats import EntryFill, EntryOrder, SideStats, fill_stats
from trading_core.lab.funding import (
    FundingPayment,
    FundingRate,
    PositionKey,
    apply_funding,
    funding_boundaries,
    own_funding_gap,
)
from trading_core.lab.loser_band import BandTrade, loser_band
from trading_core.lab.metrics import ClosedTrade, FundingCash, run_metrics
from trading_core.lab.segments import Segment, SegmentPlan, hours_of, plan_segments, required_days
from trading_core.lab.variants import BASE, VariantSpec
from trading_core.node.recording import instrument_row
from trading_core.recording import repository
from trading_core.recording.enums import (
    Env,
    EquityBasis,
    FundingSource,
    Intent,
    Liquidity,
    OrderSide,
    OrderType,
    PositionSide,
    PositionStatus,
    RunMode,
    RunStatus,
    Sample,
    StrategyKind,
)
from trading_core.recording.rows import (
    DecisionRow,
    FillRow,
    FundingRow,
    InstrumentRow,
    JsonObject,
    JsonValue,
    OrderRow,
    PositionRow,
    RiskEventRow,
    RunBundle,
    RunFillStatsRow,
    RunResultRow,
    RunRow,
    ns_to_utc,
)
from trading_core.thresholds_file import packaged_thresholds
from trading_core.versioning import FrozenHashes

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_VARIANT_FAILED: Final = 1
EXIT_MISSING_DAYS: Final = 2
EXIT_INTERRUPTED: Final = 130
LOSER_STRATEGY: Final = "random_taker"

# Where a run's funding rates come from (``run_config.funding_source``, spec 0009 AC-14).
type FundingSourceName = Literal["tardis", "own", "none"]


class Store(Protocol):
    """Where a replay keeps its rows: the laptop Postgres as ``lab_app`` (AC-11), or memory in tests."""

    def prepare(
        self,
        instrument: InstrumentRow,
        strategy: str,
        kind: StrategyKind,
        git_commit: str | None,
        config: JsonObject,
        frozen: FrozenHashes | None = None,
    ) -> UUID:
        """Ensure the instrument and strategy version rows (the strategy of ``kind``); return the version id."""
        ...

    def write_bundle(self, bundle: RunBundle) -> None:
        """Write one completed variant in one transaction."""
        ...


class PostgresStore:
    """The ``Store`` over Postgres sessions, through the spec 0002 repository."""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def prepare(
        self,
        instrument: InstrumentRow,
        strategy: str,
        kind: StrategyKind,
        git_commit: str | None,
        config: JsonObject,
        frozen: FrozenHashes | None = None,
    ) -> UUID:
        """Upsert the instrument and ensure the version with its frozen hashes (spec 0015 AC-1), in one commit."""
        with self._sessions() as session:
            repository.upsert_instrument(session, instrument)
            version_id = repository.ensure_strategy_version(session, strategy, kind, git_commit, config, frozen)
            session.commit()
        return version_id

    def write_bundle(self, bundle: RunBundle) -> None:
        """``write_run_bundle`` in its own session."""
        with self._sessions() as session:
            repository.write_run_bundle(session, bundle)


def run_funding(
    tree: SourceTree, source: str, days: Sequence[date], plan: SegmentPlan
) -> tuple[tuple[FundingRate, ...], FundingSourceName, int | None]:
    """The rates the run applies, where they come from, and the boundary an ``own`` run lacks.

    A ``tardis`` run always applies its rates. An ``own`` run applies its recorded rates only when
    every boundary inside its segments has one from the period before it; otherwise none at all,
    and the first such boundary is returned (spec 0009 AC-14).
    """
    rates = tuple(tree.funding_rates(list(days)))
    if source == "tardis":
        return rates, "tardis", None
    boundaries = [b for s in plan.segments for b in funding_boundaries(s.start_ns, s.end_ns)]
    gap = own_funding_gap(rates, boundaries)
    if gap is not None:
        log.info(
            "lab_funding_skipped",
            extra={"fields": {"boundary": ns_to_utc(gap).isoformat()}},
        )
        return (), "none", gap
    return rates, "own", None


def plan_run(
    tree: SourceTree, loaded: LoadedRun, hours_kept: Callable[[list[int]], list[int]] | None = None
) -> SegmentPlan:
    """The clean segments of the run's range (AC-4); ``hours_kept`` narrows the range's hours first (parity, AC-15)."""
    run = loaded.run
    return plan_days(tree, required_days(run.source, run.from_, run.to), hours_kept)


def plan_days(
    tree: SourceTree, days: Sequence[date], hours_kept: Callable[[list[int]], list[int]] | None = None
) -> SegmentPlan:
    """The clean segments of ``days`` in ``tree`` (AC-4), for a caller with no replay run file (candle parity)."""
    hours = hours_of(days) if hours_kept is None else hours_kept(hours_of(days))
    incomplete: set[int] = set()
    for day in days:
        incomplete |= tree.incomplete_hours(day)
    files = {hour: tree.hour_files(hour) for hour in hours}
    return plan_segments(hours, tree.gaps(list(days)), incomplete, files)


def stamp_run_id[R: DataclassInstance](rows: Iterable[R], run_id: UUID) -> list[R]:
    """The rows with the variant's run id in place of the unit placeholder (AC-12)."""
    return [replace(row, run_id=run_id) for row in rows]


def scope_risk_events(rows: Iterable[RiskEventRow], run_id: UUID) -> list[RiskEventRow]:
    """The risk events with the run id added to each ``event_key``.

    ``risk_events`` is unique on ``(env, event_key)``, and a replay's client order ids follow the
    replay clock, so a key like ``order_rejected:<client_order_id>`` repeats in every variant and in
    every rerun of the same day; the run id keeps each run's keys apart.
    """
    return [replace(row, run_id=run_id, event_key=f"{row.event_key}@{run_id}") for row in rows]


def _merged[R](
    units: Sequence[UnitResult], pick: Callable[[UnitResult], Sequence[tuple[int, R]]], ts: Callable[[R], int]
) -> list[R]:
    """A variant's rows of one kind, sorted by (segment, ts, sequence within the unit) (AC-12)."""
    keyed = [(unit.segment, ts(row), seq, n, row) for unit in units for n, (seq, row) in enumerate(pick(unit))]
    keyed.sort(key=lambda item: item[:4])
    return [item[4] for item in keyed]


def _band_trades(
    positions: Sequence[PositionRow],
    fills: Sequence[FillRow],
    spreads: dict[str, Decimal],
    funded: set[UUID],
    segment_end_orders: set[str],
) -> list[BandTrade]:
    by_position: dict[UUID, list[FillRow]] = {}
    for fill in fills:
        if fill.position_id is not None:
            by_position.setdefault(fill.position_id, []).append(fill)
    trades: list[BandTrade] = []
    for position in positions:
        if position.status != PositionStatus.CLOSED:
            continue
        entry_side = OrderSide.BUY if position.side == PositionSide.LONG else OrderSide.SELL
        own = by_position.get(position.id, [])
        entries = [f for f in own if f.side == entry_side]
        exits = [f for f in own if f.side != entry_side]
        if not entries or not exits:
            continue
        trades.append(
            BandTrade(
                qty=position.peak_qty,
                entry_notional=sum((f.quantity * f.price for f in entries), Decimal(0)),
                exit_notional=sum((f.quantity * f.price for f in exits), Decimal(0)),
                entry_spread=spreads.get(entries[0].client_order_id, Decimal(0)),
                exit_spread=spreads.get(exits[0].client_order_id, Decimal(0)),
                net_pnl=position.realized_pnl + position.funding_total,
                crosses_funding=position.id in funded,
                segment_end=any(f.client_order_id in segment_end_orders for f in exits),
            )
        )
    return trades


@dataclass(frozen=True)
class Batch:
    """What every variant of one command shares."""

    batch_id: UUID
    loaded: LoadedRun
    settings: LabSettings
    plan: SegmentPlan
    version_id: UUID
    folder: Path
    # The run's funding_rate_update rows, by time (spec 0008 AC-9). An ``own`` run has them only
    # when they cover every boundary (spec 0009 AC-14); ``funding_source`` says which applies.
    funding_rates: tuple[FundingRate, ...] = ()
    funding_source: FundingSourceName = "none"
    # The first boundary an ``own`` run's rates did not cover, so it applies none (AC-14).
    funding_skipped_ns: int | None = None


def _run_config(batch: Batch, variant: VariantSpec, *, segment_end_partial: bool = False) -> dict[str, JsonValue]:
    run = batch.loaded.run
    return {
        "batch_id": str(batch.batch_id),
        "variant": variant.name,
        "entry_mode": variant.entry_mode,
        "run_file": batch.loaded.path,
        "run_file_sha256": batch.loaded.sha256,
        "overrides": dict(batch.loaded.overrides),
        "seed": run.seed,
        "source": run.source,
        "instrument_id": run.instrument_id,
        "maker_fee": run.maker_fee,
        "taker_fee": run.taker_fee,
        "latency_ms": variant.latency_ms,
        "queue_model": "pessimistic" if variant.queue_position else "optimistic",
        "starting_balance_usd": run.starting_balance_usd,
        "leverage": run.leverage,
        "risk_limits": dict(vars(run.risk_limits())),
        "funding_source": batch.funding_source,
        "context": {
            **run.context.model_dump(),
            "liquidations_available": run.context.values(run.source).liquidations_available,
        },
        "segments": [[s.start_ns, s.end_ns] for s in batch.plan.segments],
        "skipped_hours": [[h.hour_ns, h.reason] for h in batch.plan.skipped],
        "segment_end_partial": segment_end_partial,
        "nautilus_version": version("nautilus_trader"),
        "git_commit": batch.settings.git_commit,
    }


def _strategy_config(loaded: LoadedRun, variant: VariantSpec) -> dict[str, ConfigValue]:
    """The strategy's settings with the variant's entry mode (``entry=taker`` changes only that, AC-6)."""
    config = loaded.run.strategy_values()
    if variant.entry_mode is not None:
        config["entry_mode"] = variant.entry_mode
    return config


def unit_plans(
    settings: LabSettings, loaded: LoadedRun, segments: Sequence[Segment], variant: VariantSpec, *, funding: bool
) -> list[UnitPlan]:
    """One unit per segment for ``variant``, in segment order; ``funding`` samples every boundary."""
    run = loaded.run
    return [
        UnitPlan(
            variant=variant.name,
            segment=segment,
            source_root=settings.lab_catalog_dir / run.source,
            instrument_id=run.instrument_id,
            maker_fee=run.maker_fee,
            taker_fee=run.taker_fee,
            latency_ms=variant.latency_ms,
            queue_position=variant.queue_position,
            starting_balance_usd=run.starting_balance_usd,
            leverage=run.leverage,
            risk=run.risk_limits(),
            seed=run.seed,
            strategy_name=run.strategy.name,
            strategy_config=_strategy_config(loaded, variant),
            funding=funding,
            test_strategies=settings.lab_test_strategies,
            market=run.context.values(run.source),
            context_warmup_s=run.context.warmup_s,
        )
        for segment in segments
    ]


def _unit_plans(batch: Batch, variant: VariantSpec) -> list[UnitPlan]:
    return unit_plans(
        batch.settings, batch.loaded, batch.plan.segments, variant, funding=batch.funding_source != "none"
    )


def _replayed_days(segments: Sequence[Segment]) -> list[date]:
    """Every UTC day the run replayed (a segment sits inside one day)."""
    return sorted({day_of(segment.start_ns) for segment in segments})


def build_bundle(
    batch: Batch, variant: VariantSpec, units: Sequence[UnitResult], *, started_at: datetime, ended_at: datetime
) -> tuple[RunBundle, Results, BandReport | None]:
    """Merge a variant's units, stamp its run id, and score it; nothing is written here."""
    run = batch.loaded.run
    run_id = uuid.uuid4()
    decisions = stamp_run_id(_merged(units, lambda u: u.decisions, lambda r: r.decided_ns), run_id)
    risk_events = scope_risk_events(_merged(units, lambda u: u.risk_events, lambda r: r.occurred_ns), run_id)
    orders = stamp_run_id(_merged(units, lambda u: u.orders, lambda r: r.last_event_ns), run_id)
    order_events = stamp_run_id(_merged(units, lambda u: u.order_events, lambda r: r.occurred_ns), run_id)
    fills = stamp_run_id(_merged(units, lambda u: u.fills, lambda r: r.filled_ns), run_id)
    positions = stamp_run_id(_merged(units, lambda u: u.positions, lambda r: r.opened_ns), run_id)
    spreads = {coid: spread for unit in units for coid, spread in unit.spreads.items()}
    payments = apply_funding([s for unit in units for s in unit.funding_samples], batch.funding_rates)
    positions, funding = with_funding(positions, payments, run_id, run.instrument_id)

    closed = [p for p in positions if p.status == PositionStatus.CLOSED]
    metrics = run_metrics(
        run.starting_balance_usd,
        [ClosedTrade(p.last_event_ns, p.realized_pnl, p.fees_total, p.funding_total) for p in closed],
        [FundingCash(paid_ns=f.paid_ns, amount=f.amount) for f in funding],
        sum((f.fee for f in fills), Decimal(0)),
    )
    segments = batch.plan.segments
    # A closed position's row side is FLAT, so a trade's direction comes from its first fill.
    opening: dict[UUID, bool] = {}
    for f in sorted(fills, key=lambda f: f.filled_ns):
        if f.position_id is not None:
            opening.setdefault(f.position_id, f.side == OrderSide.BUY)
    closed_positions = [
        ClosedPosition(
            opening.get(p.id, p.side == PositionSide.LONG),
            p.last_event_ns,
            p.realized_pnl,
            p.fees_total,
            p.funding_total,
        )
        for p in closed
    ]
    # Replay has no marked curve across its gaps, so its daily record is realized only (spec 0015).
    daily = realized_daily(
        _replayed_days(segments),
        run.starting_balance_usd,
        closed_positions,
        [FundingCash(paid_ns=f.paid_ns, amount=f.amount) for f in funding],
    )
    config = _run_config(batch, variant, segment_end_partial=any(u.segment_end_partial for u in units))
    run_row = RunRow(
        id=run_id,
        env=Env.DEMO,
        mode=RunMode.REPLAY,
        status=RunStatus.COMPLETED,
        strategy_version_id=batch.version_id,
        trader_id=TRADER_ID,
        run_config=config,
        data_start_at=ns_to_utc(segments[0].start_ns) if segments else None,
        data_end_at=ns_to_utc(segments[-1].end_ns) if segments else None,
        started_at=started_at,
        ended_at=ended_at,
    )
    result = RunResultRow(
        run_id=run_id,
        env=Env.DEMO,
        sample=Sample.FULL,
        return_after_costs=metrics.return_after_costs,
        max_drawdown=metrics.max_drawdown,
        risk_adjusted_ratio=daily_sharpe(daily, run.starting_balance_usd, chained=False),
        ratio_kind=RATIO_KIND,
        win_rate=metrics.win_rate,
        trade_count=metrics.trade_count,
        fees_total=metrics.fees_total,
        funding_total=metrics.funding_total,
        currency="USD",
        computed_at=ended_at,
    )
    sides = _fill_stats(units, decisions, orders)
    stats_rows = [
        RunFillStatsRow(
            run_id=run_id,
            env=Env.DEMO,
            liquidity=s.liquidity,
            orders_placed=s.orders_placed,
            orders_filled=s.orders_filled,
            fill_rate=s.fill_rate,
            fill_count=s.fill_count,
            markout_1s_bps=s.markouts[1],
            markout_5s_bps=s.markouts[5],
            markout_30s_bps=s.markouts[30],
            computed_at=ended_at,
        )
        for s in sides
    ]
    bundle = RunBundle(
        run=run_row,
        results=[result],
        decisions=decisions,
        orders=orders,
        order_events=order_events,
        fills=fills,
        positions=positions,
        funding=funding,
        risk_events=risk_events,
        fill_stats=stats_rows,
        daily_equity=daily_equity_rows(run_id, daily, EquityBasis.REALIZED),
        measures=measures_row(
            run_id, EquityBasis.REALIZED, trade_measures(closed_positions, packaged_thresholds().top_trades_quantile)
        ),
    )
    results = Results(
        net_pnl=metrics.net_pnl,
        return_after_costs=metrics.return_after_costs,
        max_drawdown=metrics.max_drawdown,
        win_rate=metrics.win_rate,
        trade_count=metrics.trade_count,
        fees_total=metrics.fees_total,
        funding_total=metrics.funding_total,
        open_positions=len(positions) - len(closed),
        fill_stats=[
            FillStatsReport(
                liquidity=s.liquidity.value,
                orders_placed=s.orders_placed,
                orders_filled=s.orders_filled,
                fill_count=s.fill_count,
                fill_rate=s.fill_rate,
                markouts_bps={f"{h}s": v for h, v in s.markouts.items()},
                left_out={f"{h}s": n for h, n in s.left_out.items()},
            )
            for s in sides
        ],
    )
    band = None
    if run.strategy.name == LOSER_STRATEGY:
        funded = {f.position_id for f in funding if f.position_id is not None}
        closes = {d.id for d in decisions if d.reason == SEGMENT_END}
        segment_end_orders = {o.client_order_id for o in orders if o.decision_id in closes}
        checked = loser_band(_band_trades(positions, fills, spreads, funded, segment_end_orders), run.taker_fee)
        band = BandReport(
            trades_used=checked.trades_used,
            total_net=checked.total_net,
            expected=checked.expected,
            mean_net=checked.mean_net,
            std_error=checked.std_error,
            low=checked.low,
            high=checked.high,
            passed=checked.passed,
        )
    return bundle, results, band


def _fill_stats(
    units: Sequence[UnitResult], decisions: Sequence[DecisionRow], orders: Sequence[OrderRow]
) -> list[SideStats]:
    """AC-10 over the variant's entry orders (orders whose decision is an ``enter``), with each
    fill's markouts taken from its own unit (trade ids repeat across segments, client order ids do not)."""
    enters = {d.id for d in decisions if d.intent == Intent.ENTER}
    entry_orders = [
        EntryOrder(
            o.client_order_id,
            Liquidity.MAKER if o.post_only or o.order_type == OrderType.LIMIT else Liquidity.TAKER,
        )
        for o in orders
        if o.decision_id in enters
    ]
    entry_ids = {o.client_order_id for o in entry_orders}
    entry_fills: list[EntryFill] = []
    for unit in units:
        marks = {m.trade_id: m.markouts for m in unit.markouts}
        entry_fills += [
            EntryFill(f.client_order_id, f.liquidity, f.quantity, marks.get(f.trade_id, {}))
            for _, f in unit.fills
            if f.client_order_id in entry_ids
        ]
    return fill_stats(entry_orders, entry_fills)


def with_funding(
    positions: Sequence[PositionRow], payments: Sequence[FundingPayment], run_id: UUID, instrument_id: str
) -> tuple[list[PositionRow], list[FundingRow]]:
    """Each payment as a ``computed`` funding row of its position, and each position's ``funding_total``.

    ``realized_pnl`` stays before funding, as in paper (AC-9).
    """
    by_key = {PositionKey(p.position_ref, p.opened_ns): p for p in positions}
    rows: list[FundingRow] = []
    totals: dict[UUID, Decimal] = {}
    for payment in payments:
        position = by_key.get(payment.position)
        if position is None:
            raise LookupError(f"funding at {payment.boundary_ns} for a position not recorded: {payment.position}")
        rows.append(
            FundingRow(
                env=Env.DEMO,
                run_id=run_id,
                position_id=position.id,
                instrument_id=instrument_id,
                amount=payment.amount,
                currency="USD",
                rate=payment.rate,
                source=FundingSource.COMPUTED,
                paid_ns=payment.boundary_ns,
            )
        )
        totals[position.id] = totals.get(position.id, Decimal(0)) + payment.amount
    updated = [replace(p, funding_total=totals[p.id]) if p.id in totals else p for p in positions]
    return updated, rows


def _report(
    batch: Batch,
    variant: VariantSpec,
    *,
    started_at: datetime,
    ended_at: datetime,
    bundle: RunBundle | None = None,
    results: Results | None = None,
    band: BandReport | None = None,
    error: Exception | None = None,
    interrupted: bool = False,
) -> LabReport:
    status: Status = "interrupted" if interrupted else "completed" if error is None else "failed"
    return LabReport(
        status=status,
        error=None if error is None else error_info(error),
        run_id=None if bundle is None else str(bundle.run.id),
        batch_id=str(batch.batch_id),
        variant=variant.name,
        strategy=batch.loaded.run.strategy.name,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        run_config=_run_config(batch, variant) if bundle is None else dict(bundle.run.run_config),
        segments=[(s.start_ns, s.end_ns) for s in batch.plan.segments],
        skipped_hours=[(h.hour_ns, h.reason) for h in batch.plan.skipped],
        results=results,
        loser_band=band,
        before_funding=batch.funding_source == "none",
        funding_skipped_ns=batch.funding_skipped_ns,
    )


def error_info(error: Exception) -> ErrorInfo:
    """The failure as the report shows it: a worker's exception keeps its own type name (AC-13)."""
    if isinstance(error, UnitFailedError):
        return ErrorInfo(type=error.error_type, message=str(error))
    return ErrorInfo(type=type(error).__name__, message=str(error))


def _failed(batch: Batch, variant: VariantSpec, error: Exception, *, started_at: datetime) -> LabReport:
    log.error(
        "lab_variant_failed",
        extra={"fields": {"variant": variant.name, "error": f"{error_info(error).type}: {error}"}},
    )
    report = _report(batch, variant, started_at=started_at, ended_at=datetime.now(UTC), error=error)
    write_variant(batch.folder, report, None)
    return report


def finish_variant(
    batch: Batch,
    variant: VariantSpec,
    units: Sequence[UnitResult],
    store: Store,
    *,
    started_at: datetime,
    ended_at: datetime,
) -> LabReport:
    """Score a variant's units, write its bundle, then its report; a failure writes only the report."""
    try:
        bundle, results, band = build_bundle(batch, variant, units, started_at=started_at, ended_at=ended_at)
        store.write_bundle(bundle)
    except Exception as exc:
        return _failed(batch, variant, exc, started_at=started_at)
    report = _report(
        batch, variant, started_at=started_at, ended_at=ended_at, bundle=bundle, results=results, band=band
    )
    write_variant(batch.folder, report, bundle)
    log.info(
        "lab_variant_done",
        extra={
            "fields": {
                "variant": variant.name,
                "run_id": str(bundle.run.id),
                "net_pnl": format(results.net_pnl, "f"),
                "trades": results.trade_count,
            }
        },
    )
    return report


@dataclass
class _VariantState:
    """What the parent knows of one variant while its units run."""

    spec: VariantSpec
    plans: int
    units: list[UnitResult] = field(default_factory=list)
    error: Exception | None = None
    ended_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        return self.error is not None or len(self.units) == self.plans


class Interrupts:
    """SIGINT and SIGTERM raise ``ReplayInterruptedError`` in the parent, except inside ``shield``.

    A signal during a write is held until the write ends, so a variant is never half written:
    its bundle and its report land together, or neither does.
    """

    def __init__(self) -> None:
        self.pending = False
        self._shielded = False

    def handle(self, signum: int, frame: FrameType | None) -> None:
        self.pending = True
        if not self._shielded:
            self._shielded = True  # a second signal during the cleanup changes nothing
            raise ReplayInterruptedError(signal.Signals(signum).name)

    def hold(self) -> None:
        """From now on a signal is only noted: the batch is already stopping."""
        self._shielded = True

    @contextmanager
    def shield(self) -> Iterator[None]:
        self._shielded = True
        try:
            yield
        finally:
            self._shielded = False
        if self.pending:
            self._shielded = True
            raise ReplayInterruptedError("interrupted during a write")


@contextmanager
def interrupt_handling(interrupts: Interrupts) -> Iterator[None]:
    previous = {sig: signal.signal(sig, interrupts.handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_batch(
    batch: Batch, variants: Sequence[VariantSpec], store: Store, *, target: Target = run_unit
) -> tuple[list[LabReport], bool]:
    """Run every variant's units in the worker pool and write each variant in order.

    Returns the reports in variant order and whether the batch was interrupted.
    """
    plans = {v.name: _unit_plans(batch, v) for v in variants}
    states: Mapping[str, _VariantState] = {v.name: _VariantState(v, len(plans[v.name])) for v in variants}
    started_at = datetime.now(UTC)
    reports: list[LabReport] = []
    interrupts = Interrupts()
    pool = UnitPool(
        [plan for v in variants for plan in plans[v.name]],
        batch.settings.lab_workers,
        target=target,
        env=batch.settings.trading_env,
    )

    def write_ready() -> None:
        while len(reports) < len(variants) and states[variants[len(reports)].name].resolved:
            state = states[variants[len(reports)].name]
            with interrupts.shield():
                if state.error is not None:
                    reports.append(_failed(batch, state.spec, state.error, started_at=started_at))
                else:
                    reports.append(
                        finish_variant(
                            batch,
                            state.spec,
                            state.units,
                            store,
                            started_at=started_at,
                            ended_at=state.ended_at or datetime.now(UTC),
                        )
                    )

    interrupted = False
    with interrupt_handling(interrupts):
        try:
            write_ready()  # a variant with no unit at all is complete at once
            for outcome in pool.outcomes():
                state = states[outcome.plan.variant]
                if state.error is not None:
                    continue
                if isinstance(outcome, UnitDone):
                    state.units.append(outcome.result)
                else:
                    state.error = outcome.error
                    pool.fail_variant(state.spec.name)
                if state.resolved:
                    state.ended_at = datetime.now(UTC)
                write_ready()
        except ReplayInterruptedError:
            interrupted = True
            log.warning("lab_batch_interrupted", extra={"fields": {"batch_id": str(batch.batch_id)}})
        finally:
            interrupts.hold()
            pool.terminate_all()
    if interrupted:
        now = datetime.now(UTC)
        for v in variants[len(reports) :]:
            report = _report(batch, v, started_at=started_at, ended_at=now, interrupted=True)
            write_variant(batch.folder, report, None)
            reports.append(report)
    return reports, interrupted


def replay(
    settings: LabSettings,
    loaded: LoadedRun,
    store: Store,
    *,
    variants: Sequence[VariantSpec] = (),
    target: Target = run_unit,
) -> int:
    """Run ``lab replay`` after validation; returns the exit code.

    ``variants`` come from ``expand_variants`` (AC-6), ``base`` first; none means ``base`` alone.
    """
    run = loaded.run
    if not variants:
        variants = [VariantSpec(BASE, run.latency_ms, True)]
    tree = SourceTree(settings.lab_catalog_dir / run.source, run.instrument_id)
    days = required_days(run.source, run.from_, run.to)
    if not days:
        log.error("lab_no_required_day", extra={"fields": {"from": run.from_.isoformat(), "to": run.to.isoformat()}})
        return EXIT_MISSING_DAYS
    missing = tree.missing_manifests(days)
    if missing:
        log.error(
            "lab_days_missing", extra={"fields": {"days": [d.isoformat() for d in missing], "root": str(tree.root)}}
        )
        return EXIT_MISSING_DAYS

    plan = plan_run(tree, loaded)
    log.info(
        "lab_segment_planned",
        extra={
            "fields": {
                "variants": [v.name for v in variants],
                "segments": len(plan.segments),
                "units": len(plan.segments) * len(variants),
                "skipped_hours": plan.skipped_by_reason(),
            }
        },
    )

    entry = registry_entry(run.strategy.name, test_strategies=settings.lab_test_strategies)
    version_id = store.prepare(
        instrument_row(pinned_instrument(run.instrument_id)),
        run.strategy.name,
        entry.kind,
        settings.git_commit,
        run.version_config(),
        entry.frozen(run.version_config()),
    )
    rates, funding_source, funding_skipped_ns = run_funding(tree, run.source, days, plan)
    batch_id = uuid.uuid7()
    batch = Batch(
        funding_rates=rates,
        funding_source=funding_source,
        funding_skipped_ns=funding_skipped_ns,
        batch_id=batch_id,
        loaded=loaded,
        settings=settings,
        plan=plan,
        version_id=version_id,
        folder=settings.lab_reports_dir / str(batch_id),
    )
    batch.folder.mkdir(parents=True, exist_ok=True)
    reports, interrupted = run_batch(batch, variants, store, target=target)
    (batch.folder / "index.md").write_text(index_markdown(str(batch_id), reports), encoding="utf-8")
    failed = [r.variant for r in reports if r.status == "failed"]
    log.info(
        "lab_batch_done",
        extra={
            "fields": {
                "batch_id": str(batch_id),
                "variants": len(reports),
                "failed": failed,
                "interrupted": interrupted,
            }
        },
    )
    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_VARIANT_FAILED if failed else EXIT_OK
