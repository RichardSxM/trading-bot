"""``lab parity <runfile> <csv>`` (spec 0008 AC-15): replay the paper loop's strategy over the paper hours and compare.

The run file gives ``[paper]``, the trader's ``strategy_config()`` verbatim. Paper runs whose
config hash differs are left out (``parity_config_mismatch`` names the keys that differ). Over the
clean own hours that lie wholly inside the other runs, ``placeholder_timed`` replays in memory
(units in the worker pool, nothing written to Postgres) and its ``enter`` decisions are matched
with the paper ones by ``trading_core.lab.parity.compare``.

Exit codes: 0 ratio at least 0.95, 1 under 0.95 or a unit failed, 2 required days missing or no
decision left to compare, 130 interrupted.
"""

import logging
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import Final, TextIO

from lab.catalog import SourceTree
from lab.paper_csv import PaperData
from lab.pool import Target, UnitDone, UnitPool
from lab.replay import EXIT_INTERRUPTED, EXIT_MISSING_DAYS, EXIT_OK, EXIT_VARIANT_FAILED, plan_run, unit_plans
from lab.runfile import LoadedRun
from lab.settings import LabSettings
from lab.units import UnitPlan, UnitResult, run_unit
from trading_core.lab.errors import ReplayInterruptedError
from trading_core.lab.parity import (
    Decided,
    ParityResult,
    compare,
    config_sha256,
    counted_windows,
    differing_keys,
    eligible_hours,
)
from trading_core.lab.segments import required_days
from trading_core.lab.variants import VariantSpec
from trading_core.recording.enums import RiskVerdict
from trading_core.recording.rows import ns_to_utc

log = logging.getLogger(__name__)

VARIANT: Final = "parity"


@contextmanager
def _interruptible() -> Iterator[None]:
    """SIGINT and SIGTERM raise ``ReplayInterruptedError``, so the pool's children are stopped."""

    def handle(signum: int, frame: FrameType | None) -> None:
        raise ReplayInterruptedError(signal.Signals(signum).name)

    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_units(plans: Sequence[UnitPlan], settings: LabSettings, target: Target) -> list[UnitResult]:
    """Every unit's result in plan order; the first failure stops the rest and is raised."""
    pool = UnitPool(plans, settings.lab_workers, target=target, env=settings.trading_env)
    results: dict[int, UnitResult] = {}
    try:
        for outcome in pool.outcomes():
            if not isinstance(outcome, UnitDone):
                raise outcome.error
            results[outcome.plan.segment.index] = outcome.result
    finally:
        pool.terminate_all()
    return [results[plan.segment.index] for plan in plans]


def _when(ns: int) -> str:
    return ns_to_utc(ns).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(source: str, d: Decided) -> str:
    verdict = "approved" if d.approved else f"rejected ({d.risk_reason})"
    return f"unmatched {source:<6} {_when(d.decided_ns)} {d.side or '-'} {verdict}"


def print_result(result: ParityResult, left_out_runs: int, out: TextIO) -> None:
    """The ratio, the counts, and every unmatched decision, one per line."""
    ratio = "n/a" if result.ratio is None else f"{result.ratio:.4f}"
    print(
        f"parity {ratio} ({result.matches} matched; paper {result.paper}, replay {result.replay}; "
        f"{len(result.excluded)} boundaries excluded; {left_out_runs} runs left out)",
        file=out,
    )
    lines = [(d.decided_ns, _line("paper", d)) for d in result.unmatched_paper]
    lines += [(d.decided_ns, _line("replay", d)) for d in result.unmatched_replay]
    for _, line in sorted(lines):
        print(line, file=out)


def parity(
    settings: LabSettings, loaded: LoadedRun, paper: PaperData, *, target: Target = run_unit, out: TextIO = sys.stdout
) -> int:
    """Run ``lab parity`` after validation; returns the exit code.

    ``loaded`` must carry ``[paper]`` (``main`` refuses a run file without one, exit 64).
    """
    run = loaded.run
    strategy = run.paper_strategy()
    if strategy is None:
        raise ValueError("lab parity needs a run file with [paper]")
    config = run.version_config()
    run_hash = config_sha256(config)
    kept = [r for r in paper.runs if r.config_sha256 == run_hash]
    for r in paper.runs:
        if r.config_sha256 != run_hash:
            log.warning(
                "parity_config_mismatch",
                extra={"fields": {"run_id": r.run_id, "keys": differing_keys(r.config, config)}},
            )

    tree = SourceTree(settings.lab_catalog_dir / run.source, run.instrument_id)
    days = required_days(run.source, run.from_, run.to)
    missing = tree.missing_manifests(days)
    if not days or missing:
        log.error(
            "lab_days_missing", extra={"fields": {"days": [d.isoformat() for d in missing], "root": str(tree.root)}}
        )
        return EXIT_MISSING_DAYS
    plan = plan_run(tree, loaded, lambda hours: eligible_hours(hours, kept, run_hash))
    log.info(
        "parity_segment_planned",
        extra={
            "fields": {
                "runs": len(paper.runs),
                "runs_left_out": len(paper.runs) - len(kept),
                "segments": len(plan.segments),
                "skipped_hours": plan.skipped_by_reason(),
            }
        },
    )

    plans = unit_plans(
        settings, loaded, plan.segments, VariantSpec(VARIANT, run.latency_ms, True), funding=run.source == "tardis"
    )
    try:
        with _interruptible():
            units = run_units(plans, settings, target)
    except ReplayInterruptedError:
        log.warning("parity_interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        log.error("parity_unit_failed", extra={"fields": {"error": f"{type(exc).__name__}: {exc}"}})
        return EXIT_VARIANT_FAILED

    replayed = [
        Decided(
            decided_ns=row.decided_ns,
            intent=row.intent,
            side=row.side,
            approved=row.risk_verdict == RiskVerdict.APPROVED,
            risk_reason=row.risk_reason,
        )
        for unit in units
        for _, row in unit.decisions
    ]
    kept_ids = {r.run_id for r in kept}
    windows = counted_windows(((s.start_ns, s.end_ns) for s in plan.segments), strategy.warmup_ns)
    result = compare([d.decided for d in paper.decisions if d.run_id in kept_ids], replayed, windows)
    print_result(result, len(paper.runs) - len(kept), out)
    log.info(
        "parity_done",
        extra={
            "fields": {
                "ratio": None if result.ratio is None else format(result.ratio, "f"),
                "matches": result.matches,
                "paper": result.paper,
                "replay": result.replay,
                "excluded": len(result.excluded),
            }
        },
    )
    if result.ratio is None:
        return EXIT_MISSING_DAYS
    return EXIT_OK if result.passed else EXIT_VARIANT_FAILED
