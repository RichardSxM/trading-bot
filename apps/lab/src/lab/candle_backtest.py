"""``lab backtest``: an hour level strategy over stored Kraken candles (spec 0014 AC-6, AC-15, AC-16, AC-17).

It checks the range (every month committed, the stored span, funding at every charge time or a
carried rate, and the warmup row) and exits 2 before anything is written when something is
missing. Then each variant runs as one candle unit in the worker pool. The parent turns each
unit's funding samples into payments with the stored rates (a missing row is charged at the
latest rate when it is at most ``FUNDING_CARRY_MAX`` old), scores it, writes the bundle as
``lab_app`` with mode ``backtest``, and writes the reports in variant order (AC-7). With
``--lookahead-check`` the first variant also runs cut at three seeded minutes (never a whole hour), and a cut run whose
decisions before the cut differ fails it (AC-19). A failed unit fails only its variant; SIGINT or
SIGTERM writes ``interrupted`` reports and exits 130 (AC-18).
"""

import logging
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Final

from lab.actors import LIQUIDATED, RANGE_END
from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan, CandleUnitResult, run_candle_unit
from lab.instruments import pinned_instrument
from lab.pool import Target, UnitDone, UnitPool
from lab.replay import (
    EXIT_INTERRUPTED,
    Interrupts,
    Store,
    error_info,
    interrupt_handling,
    scope_risk_events,
    stamp_run_id,
    with_funding,
)
from lab.report import (
    CandleReport,
    CoinReport,
    LabReport,
    MonthQuality,
    Results,
    SideSplitReport,
    Status,
    candle_index_markdown,
    entry_decision_counts,
    write_variant,
)
from lab.runfile import LoadedCandleRun
from lab.settings import LabSettings
from lab.strategies import registry_entry
from lab.study_rows import RATIO_KIND, StudyContext, daily_equity_rows, daily_sharpe, measures_row
from lab.units import TRADER_ID
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candle_metrics import (
    MarkFill,
    TradeOutcome,
    candle_measures,
    combined_equity,
    marked_equity,
    mean_benchmark,
)
from trading_core.lab.candle_path import spread
from trading_core.lab.candles import (
    FUNDING_CARRY_MAX_NS,
    CandleGap,
    FundingRow,
    Month,
    carried_rate,
    expected_charge_times,
)
from trading_core.lab.daily import ClosedPosition, marked_daily, trade_measures
from trading_core.lab.errors import FundingRateMissingError, LookaheadMismatchError, ReplayInterruptedError
from trading_core.lab.funding import FundingPayment, FundingRate, FundingSample, apply_funding
from trading_core.lab.lookahead import DecidedKey, cut_times, first_mismatch
from trading_core.lab.metrics import ClosedTrade, FundingCash, run_metrics
from trading_core.lab.variants import CandleVariantSpec, expand_candle_variants
from trading_core.marketdata.hours import HOUR_NS
from trading_core.node.bar_rule import bar_interval_ns
from trading_core.node.recording import instrument_row
from trading_core.recording.enums import (
    Env,
    EquityBasis,
    Intent,
    OrderSide,
    PositionSide,
    PositionStatus,
    RunMode,
    RunStatus,
    Sample,
)
from trading_core.recording.rows import FundingRow as BundleFundingRow
from trading_core.recording.rows import JsonValue, PositionRow, RunBundle, RunResultRow, RunRow, ns_to_utc
from trading_core.thresholds_file import packaged_thresholds

log = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_MISSING: Final = 2
FUNDING_SOURCE: Final = "kraken_export+api"
# How many uncovered charge times the refusal lists (AC-6).
LISTED_CHARGES: Final = 10
# How many cut runs ``--lookahead-check`` adds to the first variant (AC-19).
LOOKAHEAD_CUTS: Final = 3


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
    problems = span_problems(tree, start_ns, end_ns)
    if any(tree.read_manifest(m) is None for m in range_months(start_ns, end_ns)):
        return problems
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


def span_problems(tree: CandleTree, start_ns: int, end_ns: int) -> list[str]:
    """Why the stored candles do not cover the range (AC-6, AC-21), or nothing: missing months, or the
    range starting before the first stored minute's day or ending after the last fetched minute.
    A missing month is the only problem listed."""
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
    return problems


@dataclass(frozen=True)
class CandleBatch:
    """What every variant of one ``lab backtest`` shares."""

    batch_id: uuid.UUID
    loaded: LoadedCandleRun
    settings: LabSettings
    # Each variant's strategy version, by variant name (the grid values are part of its config hash, AC-7).
    version_ids: Mapping[str, uuid.UUID]
    folder: Path
    start_ns: int
    end_ns: int
    # One candles tree per coin, in sorted id order (spec 0022 AC-4).
    trees: Mapping[str, CandleTree]
    # Spec 0015: the share of trades ``run_measures.top_trades_profit_share`` counts, and the study
    # this batch belongs to (None for a plain ``lab backtest``).
    top_quantile: Decimal = Decimal("0.05")
    study: StudyContext | None = None


def candle_plan(batch: CandleBatch, variant: CandleVariantSpec) -> CandleUnitPlan:
    """The one unit of ``variant`` over the whole range (AC-8)."""
    run = batch.loaded.run
    return CandleUnitPlan(
        variant=variant.name,
        candles_root=next(iter(batch.trees.values())).root,
        instrument_ids=run.instruments,
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


def _run_config(
    batch: CandleBatch, variant: CandleVariantSpec, gaps: Mapping[str, Sequence[CandleGap]]
) -> dict[str, JsonValue]:
    run = batch.loaded.run
    # A multi coin run names its coins, bar types, and gaps per coin (spec 0022 AC-3, AC-6).
    coins: dict[str, JsonValue] = (
        {"instrument_ids": list(run.instruments)} if run.multi else {"instrument_id": run.single_instrument}
    )
    bar_types = [str(t.bar_type) for t in batch.trees.values()]
    gap_lists: dict[str, JsonValue] = {c: [[g.start_ns, g.end_ns, g.minutes] for g in gs] for c, gs in gaps.items()}
    return {
        "batch_id": str(batch.batch_id),
        "variant": variant.name,
        "run_file": batch.loaded.path,
        "run_file_sha256": batch.loaded.sha256,
        "overrides": dict(batch.loaded.overrides),
        "seed": run.seed,
        "source": "candles",
        **coins,
        "maker_fee": run.maker_fee,
        "taker_fee": run.taker_fee,
        "slippage_bps": variant.slippage_bps,
        "grid_point": dict(variant.grid_point),
        "starting_balance_usd": run.starting_balance_usd,
        "leverage": run.leverage,
        "risk_limits": dict(vars(run.risk_limits())),
        "stale_quote_s": run.stale_quote_s,
        "context": {**run.context.model_dump(), "liquidations_available": False},
        "bar_type": bar_types if run.multi else bar_types[0],
        "bar_spec": str(variant.grid_point.get("bar_spec", batch.loaded.bar_spec)),
        "funding_source": FUNDING_SOURCE,
        "candle_gaps": gap_lists if run.multi else gap_lists[run.single_instrument],
        "range": [batch.start_ns, batch.end_ns],
        "nautilus_version": version("nautilus_trader"),
        "git_commit": batch.settings.git_commit,
    }


def _gaps(batch: CandleBatch) -> dict[str, list[CandleGap]]:
    """Each coin's gaps inside the range (spec 0022 AC-6)."""
    return {
        coin: [
            g
            for m in range_months(batch.start_ns, batch.end_ns)
            for g in tree.read_gaps(m)
            if g.end_ns > batch.start_ns and g.start_ns < batch.end_ns
        ]
        for coin, tree in batch.trees.items()
    }


def _coin_months(tree: CandleTree, start_ns: int, end_ns: int) -> list[MonthQuality]:
    found = []
    for month in range_months(start_ns, end_ns):
        manifest = tree.read_manifest(month)
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


def _months(batch: CandleBatch) -> list[MonthQuality]:
    """The months' data quality, each count summed over the coins (one coin's own for a single coin run)."""
    per_coin = [_coin_months(tree, batch.start_ns, batch.end_ns) for tree in batch.trees.values()]
    return [
        MonthQuality(
            month=months[0].month,
            missing_minutes=sum(m.missing_minutes for m in months),
            quiet_minutes=sum(m.quiet_minutes for m in months),
            volume_rounded_rows=sum(m.volume_rounded_rows for m in months),
            zero_volume_trade_rows=sum(m.zero_volume_trade_rows for m in months),
            missing_funding_charges=sum(m.missing_funding_charges for m in months),
        )
        for months in zip(*per_coin, strict=True)
    ]


def coin_funding(
    batch: CandleBatch, unit: CandleUnitResult, positions: Sequence[PositionRow], run_id: uuid.UUID
) -> tuple[list[PositionRow], list[BundleFundingRow], dict[str, int]]:
    """Funding once per coin (spec 0022 AC-8): each coin's samples against its own stored rates and its
    own positions only, so a position never takes another coin's rate. Returns the positions with their
    ``funding_total`` (in their original order), the payment rows in sorted coin order, and each coin's
    charge times with no exact row."""
    updated = {p.id: p for p in positions}
    funding: list[BundleFundingRow] = []
    carried: dict[str, int] = {}
    for coin, tree in batch.trees.items():
        rows = stored_funding(tree, batch.start_ns, batch.end_ns)
        payments, coin_carried = funding_pass(unit.funding_samples[coin], rows, batch.start_ns, batch.end_ns)
        coin_positions, coin_rows = with_funding(
            [p for p in positions if p.instrument_id == coin], payments, run_id, coin
        )
        updated.update({p.id: p for p in coin_positions})
        funding += coin_rows
        carried[coin] = coin_carried
    return [updated[p.id] for p in positions], funding, carried


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
    positions, funding, carried = coin_funding(batch, unit, positions, run_id)

    closed = [p for p in positions if p.status == PositionStatus.CLOSED]
    cash = [FundingCash(paid_ns=f.paid_ns, amount=f.amount) for f in funding]
    metrics = run_metrics(
        run.starting_balance_usd,
        [ClosedTrade(p.last_event_ns, p.realized_pnl, p.fees_total, p.funding_total) for p in closed],
        cash,
        sum((f.fee for f in fills), Decimal(0)),
    )
    # A closed position's row side is FLAT, so a trade's direction comes from its first fill.
    opening: dict[uuid.UUID, bool] = {}
    for f in sorted(fills, key=lambda f: f.filled_ns):
        if f.position_id is not None:
            opening.setdefault(f.position_id, f.side == OrderSide.BUY)

    def outcomes(coin: str | None = None) -> list[TradeOutcome]:
        return [
            TradeOutcome(
                opening.get(p.id, p.side == PositionSide.LONG),
                p.opened_ns,
                p.last_event_ns if p.status == PositionStatus.CLOSED else None,
                p.realized_pnl + p.funding_total,
            )
            for p in positions
            if coin is None or p.instrument_id == coin
        ]

    # Each coin marked at its own hour closes, then one shared account curve (spec 0022 AC-9).
    curves = []
    closes: list[tuple[Decimal | None, Decimal | None]] = []
    spans: list[int] = []
    gaps = _gaps(batch)
    per_coin: dict[str, CoinReport] = {}
    for coin, tree in batch.trees.items():
        marks, first_close, last_close, first_ns, last_ns = hour_marks(
            tree, batch.start_ns, batch.end_ns, variant.slippage_bps
        )
        coin_fills = [f for f in fills if f.instrument_id == coin]
        coin_cash = [FundingCash(paid_ns=f.paid_ns, amount=f.amount) for f in funding if f.instrument_id == coin]
        coin_curve = marked_equity(
            run.starting_balance_usd,
            [MarkFill(f.filled_ns, f.side == OrderSide.BUY, f.quantity, f.price, f.fee) for f in coin_fills],
            coin_cash,
            marks,
        )
        curves.append(coin_curve)
        closes.append((first_close, last_close))
        spans += [t for t in (first_ns, last_ns) if t is not None]
        coin_metrics = run_metrics(
            run.starting_balance_usd,
            [
                ClosedTrade(p.last_event_ns, p.realized_pnl, p.fees_total, p.funding_total)
                for p in closed
                if p.instrument_id == coin
            ],
            coin_cash,
            sum((f.fee for f in coin_fills), Decimal(0)),
        )
        coin_measures = candle_measures(
            run.starting_balance_usd,
            coin_curve,
            outcomes(coin),
            batch.start_ns,
            batch.end_ns,
            first_close,
            last_close,
        )
        facts = unit.per_instrument[coin]
        per_coin[coin] = CoinReport(
            trade_count=coin_metrics.trade_count,
            net_pnl=coin_metrics.net_pnl,
            fees_total=coin_metrics.fees_total,
            funding_total=coin_metrics.funding_total,
            long=SideSplitReport(trades=coin_measures.long.trades, net_pnl=coin_measures.long.net_pnl),
            short=SideSplitReport(trades=coin_measures.short.trades, net_pnl=coin_measures.short.net_pnl),
            exposure=coin_measures.exposure,
            benchmark=coin_measures.benchmark,
            events=facts.events,
            first_trade_ns=facts.first_trade_ns,
            last_point_ns=facts.last_point_ns,
            first_candle_ns=first_ns,
            last_candle_ns=last_ns,
            gaps=[(g.start_ns, g.end_ns, g.minutes) for g in gaps[coin]],
            months=_coin_months(tree, batch.start_ns, batch.end_ns),
            carried_funding_charges=carried[coin],
            zero_volume_bars=facts.zero_volume_bars,
        )
    curve = combined_equity(run.starting_balance_usd, curves)
    measures = candle_measures(run.starting_balance_usd, curve, outcomes(), batch.start_ns, batch.end_ns, *closes[0])
    if run.multi:
        measures = replace(measures, benchmark=mean_benchmark(closes))
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
    days = marked_daily(curve, batch.start_ns, batch.end_ns, run.starting_balance_usd, closed_positions, cash)
    run_row = RunRow(
        id=run_id,
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        status=RunStatus.COMPLETED,
        strategy_version_id=batch.version_ids[variant.name],
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
        risk_adjusted_ratio=daily_sharpe(days, run.starting_balance_usd, chained=True),
        ratio_kind=RATIO_KIND,
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
        daily_equity=daily_equity_rows(run_id, days, EquityBasis.MARKED),
        measures=measures_row(
            run_id,
            EquityBasis.MARKED,
            trade_measures(closed_positions, batch.top_quantile),
            marked_max_drawdown=measures.marked_max_drawdown,
            longest_drawdown_hours=measures.longest_drawdown_hours,
            exposure=measures.exposure,
        ),
    )
    if batch.study is not None:
        bundle = batch.study.attach(bundle, variant.name)
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
        liquidations=sum(1 for d in decisions if d.intent == Intent.EXIT and d.reason == LIQUIDATED),
        ruined=unit.ruined_ns is not None,
        ruined_ns=unit.ruined_ns,
        first_decision_ns=first_decision,
        benchmark=measures.benchmark,
        first_candle_ns=min(spans, default=None),
        last_candle_ns=max(spans, default=None),
        # Every coin's gaps in time order (each coin's own list is in run_config, AC-6).
        gaps=sorted((g.start_ns, g.end_ns, g.minutes) for coin_gaps in gaps.values() for g in coin_gaps),
        months=_months(batch),
        carried_funding_charges=sum(carried.values()),
        zero_volume_bars=unit.zero_volume_bars,
        per_instrument=per_coin,
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
        entry_decisions=entry_decision_counts(bundle),
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
        return _failed(batch, variant, exc, started_at=started_at, store=store)
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


def trial_bundle(
    batch: CandleBatch, variant: CandleVariantSpec, status: RunStatus, *, started_at: datetime, ended_at: datetime
) -> RunBundle | None:
    """In a study, the run row and ``study_runs`` row of a variant that did not complete, so it still
    counts as a trial (spec 0015 AC-3); no results, daily equity, or measures. None outside a study."""
    if batch.study is None:
        return None
    run = RunRow(
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        status=status,
        strategy_version_id=batch.version_ids[variant.name],
        trader_id=TRADER_ID,
        run_config=_run_config(batch, variant, _gaps(batch)),
        data_start_at=ns_to_utc(batch.start_ns),
        data_end_at=ns_to_utc(batch.end_ns),
        started_at=started_at,
        ended_at=ended_at,
    )
    return batch.study.attach(RunBundle(run=run), variant.name)


def _write_trial(
    batch: CandleBatch, variant: CandleVariantSpec, store: Store | None, status: RunStatus, started_at: datetime
) -> RunBundle | None:
    bundle = trial_bundle(batch, variant, status, started_at=started_at, ended_at=datetime.now(UTC))
    if bundle is None or store is None:
        return None
    store.write_bundle(bundle)
    return bundle


def _failed(
    batch: CandleBatch,
    variant: CandleVariantSpec,
    error: Exception,
    *,
    started_at: datetime,
    store: Store | None = None,
) -> LabReport:
    log.error(
        "lab_variant_failed",
        extra={"fields": {"variant": variant.name, "error": f"{error_info(error).type}: {error}"}},
    )
    report = _report(batch, variant, "failed", started_at=started_at, ended_at=datetime.now(UTC), error=error)
    write_variant(batch.folder, report, _write_trial(batch, variant, store, RunStatus.FAILED, started_at))
    return report


def backtest(
    settings: LabSettings,
    loaded: LoadedCandleRun,
    store: Store,
    *,
    variants: Sequence[CandleVariantSpec] | None = None,
    lookahead_check: bool = False,
    target: Target[CandleUnitPlan, CandleUnitResult] = run_candle_unit,
    study: Callable[[Mapping[str, uuid.UUID]], StudyContext] | None = None,
    batch_id: uuid.UUID | None = None,
) -> int:
    """Run ``lab backtest`` after validation; returns the exit code.

    ``variants`` come from ``expand_candle_variants`` (checked before anything is read); the default
    is the run file's own (``base``, or its grid). ``lookahead_check`` runs the first variant again
    cut at ``LOOKAHEAD_CUTS`` times (AC-19). ``study`` builds the batch's study context from the
    variants' version ids (spec 0015: ``lab study`` and ``lab holdout``); ``batch_id`` names the
    report folder (a new id when None).
    """
    run = loaded.run
    if variants is None:
        variants = expand_candle_variants(run.slippage_bps, run.strategy.grid)
    trees = {coin: candle_tree(settings.lab_catalog_dir, coin) for coin in run.instruments}
    start_ns, end_ns = range_ns(loaded)
    problems = [
        f"{coin}: {problem}" if run.multi else problem
        for coin, tree in trees.items()
        for problem in range_problems(tree, start_ns, end_ns)
    ]
    if problems:
        for problem in problems:
            print(f"lab backtest: {problem}", file=sys.stderr)
        log.error("lab_candles_missing", extra={"fields": {"problems": problems}})
        return EXIT_MISSING
    entry = registry_entry(run.strategy.name, test_strategies=settings.lab_test_strategies)
    instruments = [instrument_row(pinned_instrument(coin)) for coin in run.instruments]
    # One strategy version per distinct config; the slippage variants share base's.
    configs = {v.name: run.version_config(v.grid_point) for v in variants}
    version_ids = {
        name: store.prepare(
            instruments, run.strategy.name, entry.kind, settings.git_commit, config, entry.frozen(config)
        )
        for name, config in configs.items()
    }
    batch_id = uuid.uuid7() if batch_id is None else batch_id
    batch = CandleBatch(
        batch_id=batch_id,
        loaded=loaded,
        settings=settings,
        version_ids=version_ids,
        folder=settings.lab_reports_dir / str(batch_id),
        start_ns=start_ns,
        end_ns=end_ns,
        trees=trees,
        top_quantile=packaged_thresholds().top_trades_quantile,
        study=None if study is None else study(version_ids),
    )
    batch.folder.mkdir(parents=True, exist_ok=True)
    log.info(
        "lab_backtest_planned",
        extra={
            "fields": {"batch_id": str(batch_id), "variants": [v.name for v in variants], "range": [start_ns, end_ns]}
        },
    )
    # Look ahead is a property of the code, not of a parameter value: only the first variant is cut (AC-19),
    # between two of its bar closes (spec 0019 AC-2).
    cuts: list[int] = []
    if lookahead_check:
        interval_ns = bar_interval_ns(str(variants[0].grid_point.get("bar_spec", loaded.bar_spec)))
        cuts = cut_times(run.seed, start_ns, end_ns, LOOKAHEAD_CUTS, interval_ns)
    reports, interrupted = run_candle_batch(batch, variants, store, cuts=cuts, target=target)
    (batch.folder / "index.md").write_text(candle_index_markdown(str(batch_id), reports), encoding="utf-8")
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
    return EXIT_FAILED if failed else EXIT_OK


@dataclass
class _VariantState:
    """What the parent knows of one variant while its units run: the full run and, when checked, its cut runs."""

    spec: CandleVariantSpec
    cuts: Sequence[int]
    full: CandleUnitResult | None = None
    cut_units: dict[int, CandleUnitResult] = field(default_factory=dict)
    error: Exception | None = None

    @property
    def resolved(self) -> bool:
        return self.error is not None or (self.full is not None and len(self.cut_units) == len(self.cuts))


def decided_keys(unit: CandleUnitResult) -> list[DecidedKey]:
    """The unit's decisions as look ahead keys, without the range end closer's (a cut run has its own)."""
    return [
        DecidedKey(d.decided_ns, d.intent, d.side, d.reason, d.risk_verdict)
        for _, d in unit.decisions
        if d.reason != RANGE_END
    ]


def _key_fields(key: DecidedKey | None) -> dict[str, JsonValue] | None:
    if key is None:
        return None
    return {
        "decided_at": ns_to_utc(key.decided_ns).isoformat(),
        "intent": key.intent,
        "side": key.side,
        "reason": key.reason,
        "verdict": key.verdict,
    }


def lookahead_error(state: _VariantState) -> LookaheadMismatchError | None:
    """Compare the full run with each cut run, earliest cut first (AC-19); the first mismatch fails the variant."""
    assert state.full is not None
    full = decided_keys(state.full)
    for cut in sorted(state.cut_units):
        mismatch = first_mismatch(full, decided_keys(state.cut_units[cut]), cut)
        if mismatch is None:
            continue
        fields = {
            "variant": state.spec.name,
            "cut": ns_to_utc(cut).isoformat(),
            "full": _key_fields(mismatch.full),
            "cut_run": _key_fields(mismatch.cut),
        }
        log.error("lab_lookahead_mismatch", extra={"fields": fields})
        return LookaheadMismatchError(
            f"cut at {fields['cut']}: the full run has {fields['full']}, the cut run has {fields['cut_run']}"
        )
    log.info(
        "lab_lookahead_passed",
        extra={"fields": {"variant": state.spec.name, "cuts": [ns_to_utc(c).isoformat() for c in sorted(state.cuts)]}},
    )
    return None


def candle_plans(batch: CandleBatch, variant: CandleVariantSpec, cuts: Sequence[int]) -> list[CandleUnitPlan]:
    """The variant's full unit, then one unit cut at each of ``cuts`` (the same data, ending at the cut)."""
    plan = candle_plan(batch, variant)
    return [plan, *(replace(plan, end_ns=cut, cut_ns=cut) for cut in cuts)]


def run_candle_batch(
    batch: CandleBatch,
    variants: Sequence[CandleVariantSpec],
    store: Store,
    *,
    cuts: Sequence[int] = (),
    target: Target[CandleUnitPlan, CandleUnitResult] = run_candle_unit,
) -> tuple[list[LabReport], bool]:
    """Run every variant in the worker pool and write each one in plan order (AC-7, AC-18).

    ``cuts`` are the look ahead cut times for the first variant (AC-19), empty without the check.
    Returns the reports in variant order and whether the batch was interrupted.
    """
    states = {v.name: _VariantState(v, cuts if i == 0 else ()) for i, v in enumerate(variants)}
    started_at = datetime.now(UTC)
    reports: list[LabReport] = []
    interrupts = Interrupts()
    pool = UnitPool(
        [plan for v in variants for plan in candle_plans(batch, v, states[v.name].cuts)],
        batch.settings.lab_workers,
        target=target,
        env=batch.settings.trading_env,
    )

    def write_ready() -> None:
        while len(reports) < len(variants) and states[variants[len(reports)].name].resolved:
            state = states[variants[len(reports)].name]
            with interrupts.shield():
                error = state.error if state.error is not None or not state.cuts else lookahead_error(state)
                if error is not None:
                    reports.append(_failed(batch, state.spec, error, started_at=started_at, store=store))
                else:
                    assert state.full is not None
                    reports.append(finish_candle_variant(batch, state.spec, state.full, store, started_at=started_at))

    interrupted = False
    with interrupt_handling(interrupts):
        try:
            for outcome in pool.outcomes():
                state = states[outcome.plan.variant]
                if state.error is not None:
                    continue
                if not isinstance(outcome, UnitDone):
                    state.error = outcome.error
                    pool.fail_variant(state.spec.name)
                elif outcome.plan.cut_ns is None:
                    state.full = outcome.result
                else:
                    state.cut_units[outcome.plan.cut_ns] = outcome.result
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
            report = _report(batch, v, "interrupted", started_at=started_at, ended_at=now)
            write_variant(batch.folder, report, _write_trial(batch, v, store, RunStatus.ABORTED, started_at))
            reports.append(report)
    return reports, interrupted
