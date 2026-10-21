"""``lab audit`` (spec 0017 AC-1, AC-2): spreads, moves, and order flow of the free Tardis development days.

It plans each day's clean hours with the replay's segment planner, reads every segment through
``SegmentStream`` (its quotes and trades only, no engine), and hands plain values to the pure
``trading_core.lab.audit``. Days run in spawned worker processes, ``lab_workers`` at a time. It
writes ``audit.md`` and ``audit.json`` under ``lab-reports/audit-<YYYYMMDD>/`` and touches no database.
"""

import json
import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide

from lab.catalog import SourceTree
from lab.replay import plan_days
from lab.settings import LabSettings
from lab.stream import SegmentStream
from lab.study import missing_days_exit
from lab.tardis_import import INSTRUMENT_ID
from trading_core.lab.audit import (
    Aggressor,
    DayAudit,
    Rate,
    Samples,
    SegmentAudit,
    Span,
    audit_json,
    mean_abs_funding,
)
from trading_core.lab.study_days import TARDIS_FIRST_DAY, firsts_of_month
from trading_core.logs import configure_logging

log = logging.getLogger(__name__)

PROCESS: Final = "lab"
_AGGRESSORS: Final[dict[AggressorSide, Aggressor]] = {
    AggressorSide.BUYER: "buy",
    AggressorSide.SELLER: "sell",
    AggressorSide.NO_AGGRESSOR: "none",
}


class AuditRangeError(ValueError):
    """The audit's range is not one it may read (exit 64); ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def audit_days(from_: date | None, to: date | None, holdout_start: date) -> list[date]:
    """Every 1st of the month from ``from_`` (default 2022-04-01) to ``to`` (default the day before the holdout).

    Raises ``AuditRangeError`` for a ``to`` on or after ``holdout_start``, a ``from_`` before the first
    Tardis day, or a range with no 1st of the month in it.
    """
    first = TARDIS_FIRST_DAY if from_ is None else from_
    last = holdout_start - timedelta(days=1) if to is None else to
    problems: list[str] = []
    if last >= holdout_start:
        problems.append(f"--to {last} is on or after holdout_start {holdout_start}: the audit never reads holdout days")
    if first < TARDIS_FIRST_DAY:
        problems.append(f"--from {first} is before {TARDIS_FIRST_DAY}, the first whole Tardis day of PF_XBTUSD")
    if first > last:
        problems.append(f"--from {first} is after --to {last}")
    if not problems and not firsts_of_month(first, last):
        problems.append(f"no 1st of the month between {first} and {last}")
    if problems:
        raise AuditRangeError(problems)
    return firsts_of_month(first, last)


def audit_day(root: Path, day: date) -> DayAudit:
    """One day's samples: every clean segment streamed, its quotes and trades fed to a ``SegmentAudit``."""
    tree = SourceTree(root, INSTRUMENT_ID)
    plan = plan_days(tree, [day])
    started = time.monotonic()
    samples = Samples()
    by_hour: dict[int, Samples] = {}
    late = 0
    for segment in plan.segments:
        audit = SegmentAudit(segment.start_ns, segment.end_ns)
        for chunk in SegmentStream(tree, segment).chunks():
            for event in chunk:
                if isinstance(event, QuoteTick):
                    audit.on_quote(
                        event.ts_init,
                        event.bid_price.as_decimal(),
                        event.ask_price.as_decimal(),
                        event.bid_size.as_decimal(),
                        event.ask_size.as_decimal(),
                    )
                elif isinstance(event, TradeTick):
                    audit.on_trade(event.ts_event, event.size.as_decimal(), _AGGRESSORS[event.aggressor_side])
        audit.close()
        samples.extend(audit.samples)
        for hour, hour_samples in audit.by_hour.items():
            by_hour.setdefault(hour, Samples()).extend(hour_samples)
        late += audit.late_trades
    rates = [Rate(r.ts_ns, r.rate) for r in tree.funding_rates([day])]
    log.info(
        "lab_audit_day",
        extra={
            "fields": {
                "day": day.isoformat(),
                "segments": len(plan.segments),
                "seconds": round(time.monotonic() - started, 1),
            }
        },
    )
    return DayAudit(
        day=day,
        samples=samples,
        by_hour=by_hour,
        clean_hours=sum(len(s.hours) for s in plan.segments),
        dropped_hours=plan.skipped_by_reason(),
        mean_abs_funding=mean_abs_funding([Span(s.start_ns, s.end_ns) for s in plan.segments], rates),
        late_trades=late,
    )


def _worker_audit_day(root: Path, day: date) -> DayAudit:
    configure_logging(process=PROCESS, env="demo")
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    return audit_day(root, day)


def run_audit(settings: LabSettings, days: list[date], fee_hurdle_k: Decimal, now: datetime) -> int:
    """Audit ``days`` and write the reports (0), or exit 2 when a day has no manifest, before reading anything."""
    root = settings.lab_catalog_dir / "tardis"
    missing = SourceTree(root, INSTRUMENT_ID).missing_manifests(days)
    if missing:
        return missing_days_exit("audit", missing)
    log.info("lab_audit_start", extra={"fields": {"days": len(days), "workers": settings.lab_workers}})
    with ProcessPoolExecutor(max_workers=settings.lab_workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_worker_audit_day, [root] * len(days), days))
    report = audit_json(results, fee_hurdle_k, now.isoformat())
    folder = settings.lab_reports_dir / f"audit-{now:%Y%m%d}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audit.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "audit.md").write_text(audit_markdown(report), encoding="utf-8")
    print(folder / "audit.md")
    log.info("lab_audit_done", extra={"fields": {"folder": str(folder)}})
    return 0


def _get(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _cell(value: object) -> str:
    return "n/a" if value is None else str(value)


def audit_markdown(report: dict[str, object]) -> str:
    """A short human summary of ``audit.json``."""
    pooled = report["pooled"]
    lines = [
        "# Data audit (spec 0017 AC-2)",
        "",
        f"Generated {report['generated_at']}, over {len(_days(report))} Tardis days "
        f"({_days(report)[0]} to {_days(report)[-1]}).",
        "",
        "## Pooled",
        "",
        "| Figure | Value |",
        "|---|---|",
    ]
    for p in ("p50", "p90", "p95", "p99"):
        lines.append(f"| spread {p} (bps) | {_cell(_get(pooled, 'spread_bps', p))} |")
    lines.append(f"| touch depth p50 (BTC) | {_cell(_get(pooled, 'touch_depth_btc_p50'))} |")
    for horizon in ("1m", "5m", "15m"):
        lines.append(
            f"| median abs {horizon} move (bps) | {_cell(_get(pooled, 'mid_moves', horizon, 'median_abs_bps'))} |"
        )
        for level in ("10.5", "15"):
            share = _get(pooled, "mid_moves", horizon, f"share_at_least_{level}_bps")
            lines.append(f"| share of {horizon} moves ≥ {level} bps | {_cell(share)} |")
    lines += [
        f"| trades per minute p50 | {_cell(_get(pooled, 'trades_per_minute_p50'))} |",
        f"| aggressive buy share of size | {_cell(_get(pooled, 'aggressive_buy_share'))} |",
        f"| abs delta per minute p50 (BTC) | {_cell(_get(pooled, 'abs_delta_btc_per_minute', 'p50'))} |",
        f"| abs delta per minute p90 (BTC) | {_cell(_get(pooled, 'abs_delta_btc_per_minute', 'p90'))} |",
        "",
        "## Reachable",
        "",
        f"Median abs 5 minute move at least {report['fee_hurdle_k']} times the entry cost:",
        "",
    ]
    costs = _mapping(report, "entry_costs_bps")
    reach = _mapping(report, "reachable")
    lines += [f"- `{name}` ({cost} bps): {_cell(reach.get(name))}" for name, cost in costs.items()]
    lines += ["", "## Per UTC hour", "", "| Hour | spread p50 (bps) | median abs 5m move (bps) |", "|---|---|---|"]
    for hour, figures in _mapping(report, "per_hour").items():
        lines.append(
            f"| {hour} | {_cell(_get(figures, 'spread_bps_p50'))} | {_cell(_get(figures, 'median_abs_5m_move_bps'))} |"
        )
    lines += [
        "",
        "## Per day",
        "",
        "| Day | clean hours | dropped | spread p95 | median abs 5m move | trades/min p50 | mean abs funding |",
        "|---|---|---|---|---|---|---|",
    ]
    for day, figures in _mapping(report, "per_day").items():
        dropped = _get(figures, "dropped_hours")
        dropped_text = ", ".join(f"{k} {v}" for k, v in dropped.items()) if isinstance(dropped, dict) else ""
        lines.append(
            f"| {day} | {_get(figures, 'clean_hours')} | {dropped_text or 'none'} "
            f"| {_cell(_get(figures, 'spread_bps', 'p95'))} "
            f"| {_cell(_get(figures, 'mid_moves', '5m', 'median_abs_bps'))} "
            f"| {_cell(_get(figures, 'trades_per_minute_p50'))} "
            f"| {_cell(_get(figures, 'mean_abs_funding_rate'))} |"
        )
    return "\n".join(lines) + "\n"


def _mapping(report: dict[str, object], key: str) -> dict[object, object]:
    value = report.get(key)
    return value if isinstance(value, dict) else {}


def _days(report: dict[str, object]) -> list[str]:
    days = report.get("days")
    return [str(d) for d in days] if isinstance(days, list) else []
