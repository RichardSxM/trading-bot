"""``lab replay``: plan the segments, run each variant's units, and write one bundle and report per variant.

Order (spec 0008 AC-3, AC-11, AC-13): the run file is already valid; the required days must all
have manifests (else exit 2, nothing written); the instrument and strategy version rows are
ensured; then every variant runs its units in plan order, and the parent merges their rows,
stamps the run id, scores them, and writes the bundle in one transaction. A variant that fails
writes no row, only a ``failed`` report. ``index.md`` is always written.

Milestone 2 runs the ``base`` variant only, with units in process.
"""

import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy.orm import Session

from lab.catalog import SourceTree
from lab.instruments import pinned_instrument
from lab.report import BandReport, ErrorInfo, LabReport, Results, index_markdown, write_variant
from lab.runfile import LoadedRun
from lab.settings import LabSettings
from lab.units import TRADER_ID, UnitPlan, UnitResult, run_unit
from trading_core.lab.loser_band import BandTrade, loser_band
from trading_core.lab.metrics import ClosedTrade, run_metrics
from trading_core.lab.segments import SegmentPlan, hours_of, plan_segments, required_days
from trading_core.node.recording import instrument_row
from trading_core.recording import repository
from trading_core.recording.enums import (
    Env,
    OrderSide,
    PositionSide,
    PositionStatus,
    RunMode,
    RunStatus,
    Sample,
    StrategyKind,
)
from trading_core.recording.rows import (
    FillRow,
    JsonValue,
    PositionRow,
    RunBundle,
    RunResultRow,
    RunRow,
    ns_to_utc,
)
from trading_core.risk.check import RiskLimits

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_VARIANT_FAILED: Final = 1
EXIT_MISSING_DAYS: Final = 2
BASE: Final = "base"
LOSER_STRATEGY: Final = "random_taker"

type RunUnit = Callable[[UnitPlan], UnitResult]


@dataclass(frozen=True)
class Variant:
    """One variant of a batch: the run with one thing changed (only ``base`` until Milestone 4)."""

    name: str
    latency_ms: int
    queue_position: bool


def plan_run(tree: SourceTree, loaded: LoadedRun) -> SegmentPlan:
    """The clean segments of the run's range (AC-4)."""
    run = loaded.run
    days = required_days(run.source, run.from_, run.to)
    hours = hours_of(days)
    incomplete: set[int] = set()
    for day in days:
        incomplete |= tree.incomplete_hours(day)
    files = {hour: tree.hour_files(hour) for hour in hours}
    return plan_segments(hours, tree.gaps(days), incomplete, files)


def _stamp[R: DataclassInstance](rows: Iterable[R], run_id: UUID) -> list[R]:
    """The rows with the variant's run id in place of the unit placeholder (AC-12)."""
    return [replace(row, run_id=run_id) for row in rows]


def _merged[R](
    units: Sequence[UnitResult], pick: Callable[[UnitResult], Sequence[tuple[int, R]]], ts: Callable[[R], int]
) -> list[R]:
    """A variant's rows of one kind, sorted by (segment, ts, sequence within the unit) (AC-12)."""
    keyed = [(unit.segment, ts(row), seq, n, row) for unit in units for n, (seq, row) in enumerate(pick(unit))]
    keyed.sort(key=lambda item: item[:4])
    return [item[4] for item in keyed]


def _band_trades(
    positions: Sequence[PositionRow], fills: Sequence[FillRow], spreads: dict[str, Decimal]
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


def _run_config(batch: Batch, variant: Variant) -> dict[str, JsonValue]:
    run = batch.loaded.run
    return {
        "batch_id": str(batch.batch_id),
        "variant": variant.name,
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
        "risk_limits": {
            "max_order_notional": run.risk.max_order_notional,
            "max_leverage": run.risk.max_leverage,
        },
        "funding_source": "tardis" if run.source == "tardis" else "none",
        "segments": [[s.start_ns, s.end_ns] for s in batch.plan.segments],
        "skipped_hours": [[h.hour_ns, h.reason] for h in batch.plan.skipped],
        "segment_end_partial": False,
        "nautilus_version": version("nautilus_trader"),
        "git_commit": batch.settings.git_commit,
    }


def _unit_plans(batch: Batch, variant: Variant) -> list[UnitPlan]:
    run = batch.loaded.run
    return [
        UnitPlan(
            variant=variant.name,
            segment=segment,
            source_root=batch.settings.lab_catalog_dir / run.source,
            instrument_id=run.instrument_id,
            maker_fee=run.maker_fee,
            taker_fee=run.taker_fee,
            latency_ms=variant.latency_ms,
            queue_position=variant.queue_position,
            starting_balance_usd=run.starting_balance_usd,
            leverage=run.leverage,
            risk=RiskLimits(max_order_notional=run.risk.max_order_notional, max_leverage=run.risk.max_leverage),
            seed=run.seed,
            strategy_name=run.strategy.name,
            strategy_config=dict(run.strategy.config),
        )
        for segment in batch.plan.segments
    ]


def build_bundle(
    batch: Batch, variant: Variant, units: Sequence[UnitResult], *, started_at: datetime, ended_at: datetime
) -> tuple[RunBundle, Results, BandReport | None]:
    """Merge a variant's units, stamp its run id, and score it; nothing is written here."""
    run = batch.loaded.run
    run_id = uuid.uuid4()
    decisions = _stamp(_merged(units, lambda u: u.decisions, lambda r: r.decided_ns), run_id)
    risk_events = _stamp(_merged(units, lambda u: u.risk_events, lambda r: r.occurred_ns), run_id)
    orders = _stamp(_merged(units, lambda u: u.orders, lambda r: r.last_event_ns), run_id)
    order_events = _stamp(_merged(units, lambda u: u.order_events, lambda r: r.occurred_ns), run_id)
    fills = _stamp(_merged(units, lambda u: u.fills, lambda r: r.filled_ns), run_id)
    positions = _stamp(_merged(units, lambda u: u.positions, lambda r: r.opened_ns), run_id)
    spreads = {coid: spread for unit in units for coid, spread in unit.spreads.items()}

    closed = [p for p in positions if p.status == PositionStatus.CLOSED]
    metrics = run_metrics(
        run.starting_balance_usd,
        [ClosedTrade(closed_ns=p.last_event_ns, realized_pnl=p.realized_pnl, fees=p.fees_total) for p in closed],
        [],
        sum((f.fee for f in fills), Decimal(0)),
    )
    segments = batch.plan.segments
    config = _run_config(batch, variant)
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
        win_rate=metrics.win_rate,
        trade_count=metrics.trade_count,
        fees_total=metrics.fees_total,
        funding_total=metrics.funding_total,
        currency="USD",
        computed_at=ended_at,
    )
    bundle = RunBundle(
        run=run_row,
        results=[result],
        decisions=decisions,
        orders=orders,
        order_events=order_events,
        fills=fills,
        positions=positions,
        risk_events=risk_events,
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
    )
    band = None
    if run.strategy.name == LOSER_STRATEGY:
        checked = loser_band(_band_trades(positions, fills, spreads), run.taker_fee)
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


def _report(
    batch: Batch,
    variant: Variant,
    *,
    started_at: datetime,
    ended_at: datetime,
    bundle: RunBundle | None = None,
    results: Results | None = None,
    band: BandReport | None = None,
    error: Exception | None = None,
) -> LabReport:
    return LabReport(
        status="completed" if error is None else "failed",
        error=None if error is None else ErrorInfo(type=type(error).__name__, message=str(error)),
        run_id=None if bundle is None else str(bundle.run.id),
        batch_id=str(batch.batch_id),
        variant=variant.name,
        strategy=batch.loaded.run.strategy.name,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        run_config=_run_config(batch, variant),
        segments=[(s.start_ns, s.end_ns) for s in batch.plan.segments],
        skipped_hours=[(h.hour_ns, h.reason) for h in batch.plan.skipped],
        results=results,
        loser_band=band,
        before_funding=batch.loaded.run.source == "own",
    )


def run_variant(
    batch: Batch, variant: Variant, sessions: Callable[[], Session], *, unit_runner: RunUnit = run_unit
) -> LabReport:
    """Run one variant's units in plan order, then write its bundle and report; a failure writes only the report."""
    started_at = datetime.now(UTC)
    try:
        units = [unit_runner(plan) for plan in _unit_plans(batch, variant)]
        ended_at = datetime.now(UTC)
        bundle, results, band = build_bundle(batch, variant, units, started_at=started_at, ended_at=ended_at)
        with sessions() as session:
            repository.write_run_bundle(session, bundle)
    except Exception as exc:
        log.exception(
            "lab_variant_failed",
            extra={"fields": {"variant": variant.name, "error": f"{type(exc).__name__}: {exc}"}},
        )
        failed = _report(batch, variant, started_at=started_at, ended_at=datetime.now(UTC), error=exc)
        write_variant(batch.folder, failed, None)
        return failed
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


def replay(
    settings: LabSettings,
    loaded: LoadedRun,
    sessions: Callable[[], Session],
    *,
    unit_runner: RunUnit = run_unit,
) -> int:
    """Run ``lab replay`` after validation; returns the exit code."""
    run = loaded.run
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
    variants = [Variant(name=BASE, latency_ms=run.latency_ms, queue_position=True)]
    log.info(
        "lab_segment_planned",
        extra={
            "fields": {
                "segments": len(plan.segments),
                "units": len(plan.segments) * len(variants),
                "skipped_hours": plan.skipped_by_reason(),
            }
        },
    )

    with sessions() as session:
        repository.upsert_instrument(session, instrument_row(pinned_instrument(run.instrument_id)))
        version_id = repository.ensure_strategy_version(
            session, run.strategy.name, StrategyKind.SCALPING, settings.git_commit, dict(run.strategy.config)
        )
        session.commit()

    batch_id = uuid.uuid7()
    batch = Batch(
        batch_id=batch_id,
        loaded=loaded,
        settings=settings,
        plan=plan,
        version_id=version_id,
        folder=settings.lab_reports_dir / str(batch_id),
    )
    reports = [run_variant(batch, variant, sessions, unit_runner=unit_runner) for variant in variants]
    batch.folder.mkdir(parents=True, exist_ok=True)
    (batch.folder / "index.md").write_text(index_markdown(str(batch_id), reports), encoding="utf-8")
    failed = [r.variant for r in reports if r.status != "completed"]
    log.info(
        "lab_batch_done", extra={"fields": {"batch_id": str(batch_id), "variants": len(reports), "failed": failed}}
    )
    return EXIT_VARIANT_FAILED if failed else EXIT_OK
