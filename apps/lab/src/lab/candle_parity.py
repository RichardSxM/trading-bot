"""``lab candle-parity <runfile> [--from D] [--to D]`` (spec 0014 AC-21): the candle path against the order book.

Over the clean ``own`` order book segments of the run file's days, the strategy runs twice in
memory over each segment's same start and end: once through the spec 0008 order book unit (the
run's fees, the pessimistic queue, ``[parity].latency_ms``) and once through the candle unit.
``trading_core.lab.candle_parity.match_enters`` matches their ``enter`` decisions. Nothing is
written to Postgres, so the command runs with ``--no-deps``.

Exit codes: 0 ratio at least 0.95, 1 under 0.95 or a unit failed, 2 own days or candle months
missing or no decision left to compare, 130 interrupted.
"""

import logging
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, TextIO

from lab.candle_backtest import range_ns, span_problems
from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan, CandleUnitResult, run_candle_unit
from lab.catalog import SourceTree
from lab.pool import Target, UnitDone, UnitPool
from lab.replay import (
    EXIT_INTERRUPTED,
    EXIT_MISSING_DAYS,
    EXIT_OK,
    EXIT_VARIANT_FAILED,
    Interrupts,
    interrupt_handling,
    plan_days,
)
from lab.runfile import LoadedCandleRun
from lab.settings import LabSettings
from lab.units import UnitPlan, UnitResult, run_unit
from trading_core.lab.candle_parity import CandleParityResult, match_enters
from trading_core.lab.errors import ReplayInterruptedError
from trading_core.lab.parity import Decided
from trading_core.lab.segments import Segment, required_days
from trading_core.recording.enums import RiskVerdict
from trading_core.recording.rows import DecisionRow, ns_to_utc

log = logging.getLogger(__name__)

BOOK: Final = "book"
CANDLES: Final = "candles"
SOURCE: Final = "own"
DEFAULT_LATENCY_MS: Final = 25

type ParityPlan = UnitPlan | CandleUnitPlan
type ParityUnit = UnitResult | CandleUnitResult


def run_parity_unit(plan: ParityPlan) -> ParityUnit:
    """The order book unit for a ``UnitPlan``, the candle unit for a ``CandleUnitPlan`` (one pool runs both)."""
    if isinstance(plan, CandleUnitPlan):
        return run_candle_unit(plan)
    return run_unit(plan)


def book_plans(settings: LabSettings, loaded: LoadedCandleRun, segments: Sequence[Segment]) -> list[UnitPlan]:
    """One order book unit per segment: the run's fees, ``queue_position`` true, latency from ``[parity]``."""
    run = loaded.run
    latency = DEFAULT_LATENCY_MS if run.parity is None else run.parity.latency_ms
    return [
        UnitPlan(
            variant=BOOK,
            segment=segment,
            source_root=settings.lab_catalog_dir / SOURCE,
            instrument_id=run.instrument_id,
            maker_fee=run.maker_fee,
            taker_fee=run.taker_fee,
            latency_ms=latency,
            queue_position=True,
            starting_balance_usd=run.starting_balance_usd,
            leverage=run.leverage,
            risk=run.risk_limits(),
            seed=run.seed,
            strategy_name=run.strategy.name,
            strategy_config=dict(run.strategy.config),
            funding=False,
            test_strategies=settings.lab_test_strategies,
            market=run.market(),
        )
        for segment in segments
    ]


def candle_plans(settings: LabSettings, loaded: LoadedCandleRun, segments: Sequence[Segment]) -> list[CandleUnitPlan]:
    """One candle unit per segment, over the segment's own first and last nanosecond (AC-21)."""
    run = loaded.run
    tree = candle_tree(settings.lab_catalog_dir, run.instrument_id)
    return [
        CandleUnitPlan(
            variant=CANDLES,
            candles_root=tree.root,
            instrument_id=run.instrument_id,
            start_ns=segment.start_ns,
            end_ns=segment.end_ns + 1,
            maker_fee=run.maker_fee,
            taker_fee=run.taker_fee,
            slippage_bps=run.slippage_bps,
            starting_balance_usd=run.starting_balance_usd,
            leverage=run.leverage,
            risk=run.risk_limits(),
            seed=run.seed,
            strategy_name=run.strategy.name,
            strategy_config=dict(run.strategy.config),
            stale_quote_s=Decimal(run.stale_quote_s),
            test_strategies=settings.lab_test_strategies,
            market=run.market(),
        )
        for segment in segments
    ]


def _start(plan: ParityPlan) -> int:
    return plan.start_ns if isinstance(plan, CandleUnitPlan) else plan.segment.start_ns


def run_both(
    plans: Sequence[ParityPlan], settings: LabSettings, target: Target[ParityPlan, ParityUnit]
) -> list[ParityUnit]:
    """Every unit's result in plan order; the first failure stops the rest and is raised."""
    pool: UnitPool[ParityPlan, ParityUnit] = UnitPool(
        plans, settings.lab_workers, target=target, env=settings.trading_env
    )
    results: dict[tuple[str, int], ParityUnit] = {}
    try:
        for outcome in pool.outcomes():
            if not isinstance(outcome, UnitDone):
                raise outcome.error
            results[outcome.plan.variant, _start(outcome.plan)] = outcome.result
    finally:
        pool.terminate_all()
    return [results[plan.variant, _start(plan)] for plan in plans]


def _decided(rows: Sequence[tuple[int, DecisionRow]]) -> list[Decided]:
    return [
        Decided(
            decided_ns=row.decided_ns,
            intent=row.intent,
            side=row.side,
            approved=row.risk_verdict == RiskVerdict.APPROVED,
            risk_reason=row.risk_reason,
        )
        for _, row in rows
    ]


def _when(ns: int) -> str:
    return ns_to_utc(ns).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(source: str, d: Decided) -> str:
    verdict = "approved" if d.approved else f"rejected ({d.risk_reason})"
    return f"unmatched {source:<7} {_when(d.decided_ns)} {d.side or '-'} {verdict}"


def print_result(result: CandleParityResult, segments: int, out: TextIO) -> None:
    """The ratio, the counts, and every unmatched decision, one per line in time order."""
    ratio = "n/a" if result.ratio is None else f"{result.ratio:.4f}"
    print(
        f"candle parity {ratio} ({result.matches} matched; book {result.book}, candles {result.candles}; "
        f"{segments} segments)",
        file=out,
    )
    lines = [(d.decided_ns, _line(BOOK, d)) for d in result.unmatched_book]
    lines += [(d.decided_ns, _line(CANDLES, d)) for d in result.unmatched_candles]
    for _, line in sorted(lines):
        print(line, file=out)


def candle_parity(
    settings: LabSettings,
    loaded: LoadedCandleRun,
    *,
    target: Target[ParityPlan, ParityUnit] = run_parity_unit,
    out: TextIO = sys.stdout,
) -> int:
    """Run ``lab candle-parity`` after validation; returns the exit code."""
    run = loaded.run
    tree = SourceTree(settings.lab_catalog_dir / SOURCE, run.instrument_id)
    days = required_days(SOURCE, run.from_, run.to)
    missing = tree.missing_manifests(days)
    start_ns, end_ns = range_ns(loaded)
    problems = span_problems(candle_tree(settings.lab_catalog_dir, run.instrument_id), start_ns, end_ns)
    if not days or missing or problems:
        if missing:
            print(f"lab candle-parity: no own day for {', '.join(d.isoformat() for d in missing)}", file=sys.stderr)
        for problem in problems:
            print(f"lab candle-parity: {problem}", file=sys.stderr)
        log.error(
            "lab_days_missing",
            extra={"fields": {"days": [d.isoformat() for d in missing], "candles": problems, "root": str(tree.root)}},
        )
        return EXIT_MISSING_DAYS
    plan = plan_days(tree, days)
    log.info(
        "candle_parity_planned",
        extra={
            "fields": {
                "segments": len(plan.segments),
                "skipped_hours": plan.skipped_by_reason(),
                "days": [days[0].isoformat(), days[-1].isoformat()],
            }
        },
    )
    if not plan.segments:
        print("lab candle-parity: no clean own segment in the range", file=sys.stderr)
        return EXIT_MISSING_DAYS
    plans: list[ParityPlan] = [
        *book_plans(settings, loaded, plan.segments),
        *candle_plans(settings, loaded, plan.segments),
    ]
    try:
        with interrupt_handling(Interrupts()):
            units = run_both(plans, settings, target)
    except ReplayInterruptedError:
        log.warning("candle_parity_interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        log.error("candle_parity_unit_failed", extra={"fields": {"error": f"{type(exc).__name__}: {exc}"}})
        return EXIT_VARIANT_FAILED

    book = [d for p, u in zip(plans, units, strict=True) if p.variant == BOOK for d in _decided(u.decisions)]
    candles = [d for p, u in zip(plans, units, strict=True) if p.variant == CANDLES for d in _decided(u.decisions)]
    result = match_enters(book, candles)
    print_result(result, len(plan.segments), out)
    log.info(
        "candle_parity_done",
        extra={
            "fields": {
                "ratio": None if result.ratio is None else format(result.ratio, "f"),
                "matches": result.matches,
                "book": result.book,
                "candles": result.candles,
                "segments": len(plan.segments),
            }
        },
    )
    if result.ratio is None:
        return EXIT_MISSING_DAYS
    return EXIT_OK if result.passed else EXIT_VARIANT_FAILED
