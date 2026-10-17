"""``lab backtest``: an hour level strategy over stored Kraken candles (spec 0014 AC-6, AC-15, AC-16, AC-17).

It checks the range (every month committed, the stored span, funding at every charge time or a
carried rate, and the warmup row) and exits 2 before anything is written when something is
missing. Then each variant runs as one candle unit in the worker pool. The parent turns each
unit's funding samples into payments with the stored rates (a missing row is charged at the
latest rate when it is at most ``FUNDING_CARRY_MAX`` old), scores it, writes the bundle as
``lab_app`` with mode ``backtest``, and writes the reports in variant order.
"""

import logging
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Final

from lab.actors import RANGE_END
from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan, CandleUnitResult, run_candle_unit
from lab.instruments import pinned_instrument
from lab.pool import Target, UnitDone, UnitPool
from lab.replay import Store, error_info, scope_risk_events, stamp_run_id, with_funding
from lab.report import (
    CandleReport,
    LabReport,
    MonthQuality,
    Results,
    SideSplitReport,
    Status,
    candle_index_markdown,
    write_variant,
)
from lab.runfile import LoadedCandleRun
from lab.settings import LabSettings
from lab.strategies import registry_entry
from lab.units import TRADER_ID
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candle_metrics import MarkFill, TradeOutcome, candle_measures, marked_equity
from trading_core.lab.candle_path import spread
from trading_core.lab.candles import (
    FUNDING_CARRY_MAX_NS,
    CandleGap,
    FundingRow,
    Month,
    carried_rate,
    expected_charge_times,
)
from trading_core.lab.errors import FundingRateMissingError
from trading_core.lab.funding import FundingPayment, FundingRate, FundingSample, apply_funding
from trading_core.lab.metrics import ClosedTrade, FundingCash, run_metrics
from trading_core.lab.variants import BASE, CandleVariantSpec
from trading_core.marketdata.hours import HOUR_NS
from trading_core.node.recording import instrument_row
from trading_core.recording.enums import (
    Env,
    Intent,
    OrderSide,
    PositionSide,
    PositionStatus,
    RunMode,
    RunStatus,
    Sample,
)
from trading_core.recording.rows import JsonValue, RunBundle, RunResultRow, RunRow, ns_to_utc

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_MISSING: Final = 2
FUNDING_SOURCE: Final = "kraken_export+api"
# How many uncovered charge times the refusal lists (AC-6).
LISTED_CHARGES: Final = 10


def range_ns(loaded: LoadedCandleRun) -> tuple[int, int]:
    """The range's first nanosecond (``from`` 00:00 UTC) and the close of its last minute (``to`` + 1 day)."""
    run = loaded.run
    start = datetime(run.from_.year, run.from_.month, run.from_.day, tzinfo=UTC)
    end = datetime(run.to.year, run.to.month, run.to.day, tzinfo=UTC) + timedelta(days=1)
    return int(start.timestamp()) * 1_000_000_000, int(end.timestamp()) * 1_000_000_000


def range_months(start_ns: int, end_ns: int) -> list[Month]:
    """The months whose candles the range touches."""
    months = [Month.of_ns(start_ns)]
    while months[-1] < Month.of_ns(end_ns - 1):
        months.append(months[-1].next())
    return months


def stored_funding(tree: CandleTree, start_ns: int, end_ns: int) -> list[FundingRow]:
    """Every stored funding row from the carry limit before the range to its end, by charge time."""
    months = range_months(start_ns - FUNDING_CARRY_MAX_NS, end_ns + 1)
    return sorted((r for m in months for r in tree.read_funding(m)), key=lambda r: r.charge_ns)


def range_problems(tree: CandleTree, start_ns: int, end_ns: int) -> list[str]:
    """Why the range cannot run (AC-6), or nothing: missing months, the stored span, and funding coverage."""
    months = range_months(start_ns, end_ns)
    manifests = {m: tree.read_manifest(m) for m in months}
    missing = [str(m) for m, manifest in manifests.items() if manifest is None]
    if missing:
        return [f"no committed candles for {', '.join(missing)}; run lab fetch-candles"]
    problems: list[str] = []
    first = manifests[months[0]]
    last = manifests[months[-1]]
    assert first is not None
    assert last is not None
    if first.first_ts_ns is None or Month.of_ns(first.first_ts_ns) != months[0]:
        problems.append(f"{months[0]} has no stored minute")
    elif (first.first_ts_ns - 60_000_000_000) // (24 * HOUR_NS) > start_ns // (24 * HOUR_NS):
        problems.append(f"the range starts before the first stored minute {ns_to_utc(first.first_ts_ns).isoformat()}")
    if last.last_ts_ns is None or last.last_ts_ns < end_ns:
        fetched = "none" if last.last_ts_ns is None else ns_to_utc(last.last_ts_ns).isoformat()
        problems.append(f"the range ends after the last fetched minute ({fetched})")
    rows = stored_funding(tree, start_ns, end_ns)
    uncovered = [t for t in expected_charge_times(start_ns, end_ns) if carried_rate(rows, t) is None]
    if uncovered:
        listed = ", ".join(ns_to_utc(t).isoformat() for t in uncovered[:LISTED_CHARGES])
        problem = f"{len(uncovered)} funding charge times have no rate within 3 h, first: {listed}"
        first_api = next((r.charge_ns for r in rows if r.source == "api"), None)
        if first_api is None or uncovered[0] < first_api:
            problem += "; run lab import-funding with Kraken's funding export"
        problems.append(problem)
    if not any(start_ns - FUNDING_CARRY_MAX_NS <= r.charge_ns <= start_ns for r in rows):
        problems.append("no funding row in the 3 h before the range start to warm the strategy's market context")
    return problems


@dataclass(frozen=True)
class CandleBatch:
    """What every variant of one ``lab backtest`` shares."""

    batch_id: uuid.UUID
    loaded: LoadedCandleRun
    settings: LabSettings
    version_id: uuid.UUID
    folder: Path
    start_ns: int
    end_ns: int
    tree: CandleTree


def candle_plan(batch: CandleBatch, variant: CandleVariantSpec) -> CandleUnitPlan:
    """The one unit of ``variant`` over the whole range (AC-8)."""
    run = batch.loaded.run
    return CandleUnitPlan(
        variant=variant.name,
        candles_root=batch.tree.root,
        instrument_id=run.instrument_id,
        start_ns=batch.start_ns,
        end_ns=batch.end_ns,
        maker_fee=run.maker_fee,
        taker_fee=run.taker_fee,
        slippage_bps=variant.slippage_bps,
        starting_balance_usd=run.starting_balance_usd,
        leverage=run.leverage,
        risk=run.risk_limits(),
        seed=run.seed,
        strategy_name=run.strategy.name,
        strategy_config={**run.strategy.config, **variant.grid_point},
        stale_quote_s=Decimal(run.stale_quote_s),
        test_strategies=batch.settings.lab_test_strategies,
        market=run.market(),
    )


def funding_pass(
    samples: Sequence[FundingSample], rows: Sequence[FundingRow], start_ns: int, end_ns: int
) -> tuple[list[FundingPayment], int]:
    """The payments at the charge times (AC-15), each at the rate ``rate_at`` picks after checking it is at
    most ``FUNDING_CARRY_MAX`` old, and ``carried_funding_charges``: the charge times with no exact row.

    Raises ``FundingRateMissingError`` for a sample with no rate inside the carry limit (fails the variant).
    """
    for sample in samples:
        if carried_rate(rows, sample.boundary_ns) is None:
            raise FundingRateMissingError(f"no funding rate within 3 h of {ns_to_utc(sample.boundary_ns).isoformat()}")
    exact = {r.charge_ns for r in rows}
    carried = sum(t not in exact for t in expected_charge_times(start_ns, end_ns))
    return apply_funding(samples, [FundingRate(r.charge_ns, r.rate) for r in rows]), carried


def hour_marks(
    tree: CandleTree, start_ns: int, end_ns: int, slippage_bps: Decimal
) -> tuple[list[tuple[int, Decimal]], Decimal | None, Decimal | None, int | None, int | None]:
    """The mid at every hour close in the range (the last minute's close inside its spread), and the
    range's first and last candle close and time, for the benchmark and the data quality span."""
    tick = tree.instrument.price_increment.as_decimal()
    marks: list[tuple[int, Decimal]] = []
    first_close = last_close = None
    first_ns = last_ns = None
    hour = start_ns - start_ns % HOUR_NS + HOUR_NS
    for month in range_months(start_ns, end_ns):
        for candle in tree.read_bars(month):
            if candle.open_ns < start_ns or candle.ts_ns > end_ns:
                continue
            while candle.ts_ns > hour and last_close is not None:
                bid, ask = spread(last_close, slippage_bps, tick)
                marks.append((hour, (bid + ask) / 2))
                hour += HOUR_NS
            if first_close is None:
                first_close, first_ns = candle.close, candle.ts_ns
            last_close, last_ns = candle.close, candle.ts_ns
    while last_close is not None and hour <= end_ns:
        bid, ask = spread(last_close, slippage_bps, tick)
        marks.append((hour, (bid + ask) / 2))
        hour += HOUR_NS
    return marks, first_close, last_close, first_ns, last_ns


def _run_config(batch: CandleBatch, variant: CandleVariantSpec, gaps: Sequence[CandleGap]) -> dict[str, JsonValue]:
    run = batch.loaded.run
    return {
        "batch_id": str(batch.batch_id),
        "variant": variant.name,
        "run_file": batch.loaded.path,
        "run_file_sha256": batch.loaded.sha256,
        "overrides": dict(batch.loaded.overrides),
        "seed": run.seed,
        "source": "candles",
        "instrument_id": run.instrument_id,
        "maker_fee": run.maker_fee,
        "taker_fee": run.taker_fee,
        "slippage_bps": variant.slippage_bps,
        "grid_point": dict(variant.grid_point),
        "starting_balance_usd": run.starting_balance_usd,
        "leverage": run.leverage,
        "risk_limits": dict(vars(run.risk_limits())),
        "stale_quote_s": run.stale_quote_s,
        "context": {**run.context.model_dump(), "liquidations_available": False},
        "bar_type": str(batch.tree.bar_type),
        "bar_spec": batch.loaded.bar_spec,
        "funding_source": FUNDING_SOURCE,
        "candle_gaps": [[g.start_ns, g.end_ns, g.minutes] for g in gaps],
        "range": [batch.start_ns, batch.end_ns],
        "nautilus_version": version("nautilus_trader"),
        "git_commit": batch.settings.git_commit,
    }


def _gaps(batch: CandleBatch) -> list[CandleGap]:
    return [
        g
        for m in range_months(batch.start_ns, batch.end_ns)
        for g in batch.tree.read_gaps(m)
        if g.end_ns > batch.start_ns and g.start_ns < batch.end_ns
    ]


def _months(batch: CandleBatch) -> list[MonthQuality]:
    found = []
    for month in range_months(batch.start_ns, batch.end_ns):
        manifest = batch.tree.read_manifest(month)
        assert manifest is not None  # checked before the batch started
        found.append(
            MonthQuality(
                month=manifest.month,
                missing_minutes=manifest.missing_minutes,
                quiet_minutes=manifest.quiet_minutes,
                volume_rounded_rows=manifest.volume_rounded_rows,
                zero_volume_trade_rows=manifest.zero_volume_trade_rows,
                missing_funding_charges=manifest.missing_funding_charges,
            )
        )
    return found


def build_candle_bundle(
    batch: CandleBatch, variant: CandleVariantSpec, unit: CandleUnitResult, *, started_at: datetime, ended_at: datetime
) -> tuple[RunBundle, Results, CandleReport]:
    """Stamp the unit's rows with the variant's run id, apply funding, and score it; nothing is written here."""
    run = batch.loaded.run
    run_id = uuid.uuid4()
    decisions = stamp_run_id((r for _, r in unit.decisions), run_id)
    risk_events = scope_risk_events((r for _, r in unit.risk_events), run_id)
    orders = stamp_run_id((r for _, r in unit.orders), run_id)
    order_events = stamp_run_id((r for _, r in unit.order_events), run_id)
    fills = stamp_run_id((r for _, r in unit.fills), run_id)
    positions = stamp_run_id((r for _, r in unit.positions), run_id)
    rows = stored_funding(batch.tree, batch.start_ns, batch.end_ns)
    payments, carried = funding_pass(unit.funding_samples, rows, batch.start_ns, batch.end_ns)
    positions, funding = with_funding(positions, payments, run_id, run.instrument_id)

    closed = [p for p in positions if p.status == PositionStatus.CLOSED]
    cash = [FundingCash(paid_ns=f.paid_ns, amount=f.amount) for f in funding]
    metrics = run_metrics(
        run.starting_balance_usd,
        [ClosedTrade(p.last_event_ns, p.realized_pnl, p.fees_total, p.funding_total) for p in closed],
        cash,
        sum((f.fee for f in fills), Decimal(0)),
    )
    marks, first_close, last_close, first_ns, last_ns = hour_marks(
        batch.tree, batch.start_ns, batch.end_ns, variant.slippage_bps
    )
    curve = marked_equity(
        run.starting_balance_usd,
        [MarkFill(f.filled_ns, f.side == OrderSide.BUY, f.quantity, f.price, f.fee) for f in fills],
        cash,
        marks,
    )
    # A closed position's row side is FLAT, so a trade's direction comes from its first fill.
    opening: dict[uuid.UUID, bool] = {}
    for f in sorted(fills, key=lambda f: f.filled_ns):
        if f.position_id is not None:
            opening.setdefault(f.position_id, f.side == OrderSide.BUY)
    trades = [
        TradeOutcome(
            opening.get(p.id, p.side == PositionSide.LONG),
            p.opened_ns,
            p.last_event_ns if p.status == PositionStatus.CLOSED else None,
            p.realized_pnl + p.funding_total,
        )
        for p in positions
    ]
    measures = candle_measures(
        run.starting_balance_usd, curve, trades, batch.start_ns, batch.end_ns, first_close, last_close
    )
    gaps = _gaps(batch)
    run_row = RunRow(
        id=run_id,
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        status=RunStatus.COMPLETED,
        strategy_version_id=batch.version_id,
        trader_id=TRADER_ID,
        run_config=_run_config(batch, variant, gaps),
        data_start_at=ns_to_utc(batch.start_ns),
        data_end_at=ns_to_utc(batch.end_ns),
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
        funding=funding,
        risk_events=risk_events,
        fill_stats=[],
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
    first_decision = next((d.decided_ns for d in decisions if d.reason != RANGE_END), None)
    candle = CandleReport(
        range=(batch.start_ns, batch.end_ns),
        marked_max_drawdown=measures.marked_max_drawdown,
        longest_drawdown_hours=measures.longest_drawdown_hours,
        long=SideSplitReport(trades=measures.long.trades, net_pnl=measures.long.net_pnl),
        short=SideSplitReport(trades=measures.short.trades, net_pnl=measures.short.net_pnl),
        exposure=measures.exposure,
        profit_factor=measures.profit_factor,
        expectancy=measures.expectancy,
        liquidations=sum(1 for d in decisions if d.intent == Intent.EXIT and d.reason == "liquidated"),
        ruined=False,
        first_decision_ns=first_decision,
        benchmark=measures.benchmark,
        first_candle_ns=first_ns,
        last_candle_ns=last_ns,
        gaps=[(g.start_ns, g.end_ns, g.minutes) for g in gaps],
        months=_months(batch),
        carried_funding_charges=carried,
        zero_volume_bars=unit.zero_volume_bars,
    )
    return bundle, results, candle


def _report(
    batch: CandleBatch,
    variant: CandleVariantSpec,
    status: Status,
    *,
    started_at: datetime,
    ended_at: datetime,
    bundle: RunBundle | None = None,
    results: Results | None = None,
    candle: CandleReport | None = None,
    error: Exception | None = None,
) -> LabReport:
    return LabReport(
        status=status,
        error=None if error is None else error_info(error),
        run_id=None if bundle is None else str(bundle.run.id),
        batch_id=str(batch.batch_id),
        variant=variant.name,
        strategy=batch.loaded.run.strategy.name,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        run_config=_run_config(batch, variant, _gaps(batch)) if bundle is None else dict(bundle.run.run_config),
        segments=[(batch.start_ns, batch.end_ns)],
        skipped_hours=[],
        results=results,
        before_funding=False,
        candle=candle,
    )


def finish_candle_variant(
    batch: CandleBatch,
    variant: CandleVariantSpec,
    unit: CandleUnitResult,
    store: Store,
    *,
    started_at: datetime,
) -> LabReport:
    """Score a variant, write its bundle, then its report; a failure writes only the report (AC-18)."""
    ended_at = datetime.now(UTC)
    try:
        bundle, results, candle = build_candle_bundle(batch, variant, unit, started_at=started_at, ended_at=ended_at)
        store.write_bundle(bundle)
    except Exception as exc:
        return _failed(batch, variant, exc, started_at=started_at)
    report = _report(
        batch,
        variant,
        "completed",
        started_at=started_at,
        ended_at=ended_at,
        bundle=bundle,
        results=results,
        candle=candle,
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
                "events": unit.events,
                "wall_seconds": round(unit.wall_seconds, 1),
                "ru_maxrss": unit.max_rss_bytes,
            }
        },
    )
    return report


def _failed(batch: CandleBatch, variant: CandleVariantSpec, error: Exception, *, started_at: datetime) -> LabReport:
    log.error(
        "lab_variant_failed",
        extra={"fields": {"variant": variant.name, "error": f"{error_info(error).type}: {error}"}},
    )
    report = _report(batch, variant, "failed", started_at=started_at, ended_at=datetime.now(UTC), error=error)
    write_variant(batch.folder, report, None)
    return report


def backtest(
    settings: LabSettings,
    loaded: LoadedCandleRun,
    store: Store,
    *,
    target: Target[CandleUnitPlan, CandleUnitResult] = run_candle_unit,
) -> int:
    """Run ``lab backtest`` after validation; returns the exit code."""
    run = loaded.run
    tree = candle_tree(settings.lab_catalog_dir)
    start_ns, end_ns = range_ns(loaded)
    problems = range_problems(tree, start_ns, end_ns)
    if problems:
        for problem in problems:
            print(f"lab backtest: {problem}", file=sys.stderr)
        log.error("lab_candles_missing", extra={"fields": {"problems": problems}})
        return EXIT_MISSING
    variants = [CandleVariantSpec(BASE, run.slippage_bps)]
    entry = registry_entry(run.strategy.name, test_strategies=settings.lab_test_strategies)
    version_id = store.prepare(
        instrument_row(pinned_instrument(run.instrument_id)),
        run.strategy.name,
        entry.kind,
        settings.git_commit,
        dict(run.strategy.config),
    )
    batch_id = uuid.uuid7()
    batch = CandleBatch(
        batch_id=batch_id,
        loaded=loaded,
        settings=settings,
        version_id=version_id,
        folder=settings.lab_reports_dir / str(batch_id),
        start_ns=start_ns,
        end_ns=end_ns,
        tree=tree,
    )
    batch.folder.mkdir(parents=True, exist_ok=True)
    log.info(
        "lab_backtest_planned",
        extra={
            "fields": {"batch_id": str(batch_id), "variants": [v.name for v in variants], "range": [start_ns, end_ns]}
        },
    )
    started_at = datetime.now(UTC)
    pool = UnitPool(
        [candle_plan(batch, v) for v in variants], settings.lab_workers, target=target, env=settings.trading_env
    )
    by_name = {v.name: v for v in variants}
    done: dict[str, LabReport] = {}
    try:
        for outcome in pool.outcomes():
            variant = by_name[outcome.plan.variant]
            if isinstance(outcome, UnitDone):
                done[variant.name] = finish_candle_variant(batch, variant, outcome.result, store, started_at=started_at)
            else:
                done[variant.name] = _failed(batch, variant, outcome.error, started_at=started_at)
    finally:
        pool.terminate_all()
    reports = [done[v.name] for v in variants]
    (batch.folder / "index.md").write_text(candle_index_markdown(str(batch_id), reports), encoding="utf-8")
    failed = [r.variant for r in reports if r.status == "failed"]
    log.info(
        "lab_batch_done", extra={"fields": {"batch_id": str(batch_id), "variants": len(reports), "failed": failed}}
    )
    return EXIT_FAILED if failed else EXIT_OK
