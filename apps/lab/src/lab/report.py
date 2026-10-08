"""Reports of a replay batch (spec 0008 AC-11, AC-13).

Per variant JSON, Markdown, and the exact bundle, plus the batch's ``index.md``.

``LabReport`` is dumped with ``model_dump(mode="json")``: Decimals as strings, nanosecond times
as integers, keys sorted. The Markdown renders the same model. Nothing that differs between two
runs of the same inputs goes in, except ``run_id``, ``batch_id``, ``started_at``, and
``ended_at`` (AC-12).
"""

import dataclasses
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trading_core.recording.rows import JsonValue, RunBundle

Status = Literal["completed", "failed", "interrupted"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorInfo(_Model):
    """Why a variant failed."""

    type: str
    message: str


class Results(_Model):
    """The variant's ``run_results`` numbers, plus its net P&L."""

    net_pnl: Decimal
    return_after_costs: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trade_count: int
    fees_total: Decimal
    funding_total: Decimal
    # Positions still open when the data ended; Milestone 4's segment end close brings this to 0.
    open_positions: int


class BandReport(_Model):
    """The loser check (AC-14), printed for ``random_taker`` runs."""

    trades_used: int
    total_net: Decimal
    expected: Decimal | None
    mean_net: Decimal | None
    std_error: Decimal | None
    low: Decimal | None
    high: Decimal | None
    passed: bool


class LabReport(_Model):
    """One variant's report."""

    status: Status
    error: ErrorInfo | None = None
    run_id: str | None = None
    batch_id: str
    variant: str
    strategy: str
    started_at: str
    ended_at: str
    run_config: dict[str, JsonValue]
    segments: list[tuple[int, int]]
    skipped_hours: list[tuple[int, str]]
    results: Results | None = None
    loser_band: BandReport | None = None
    before_funding: bool


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def report_json(report: LabReport) -> str:
    """The report as JSON with sorted keys."""
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def bundle_json(bundle: RunBundle) -> str:
    """The exact bundle written to Postgres, in the report's encoding, for feature 15."""
    return json.dumps(dataclasses.asdict(bundle), indent=2, sort_keys=True, default=_plain) + "\n"


def _money(value: Decimal | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{places}f}"


def _pct(value: Decimal) -> str:
    return f"{value * 100:.3f} %"


def report_markdown(report: LabReport) -> str:
    """The report as a short Markdown page."""
    lines = [f"# Replay `{report.variant}` · {report.strategy}", ""]
    lines.append(f"- Status: **{report.status}**")
    if report.error is not None:
        lines.append(f"- Error: `{report.error.type}`: {report.error.message}")
    lines += [
        f"- Batch: `{report.batch_id}`",
        f"- Run: `{report.run_id or 'not written'}`",
        f"- Source: {report.run_config.get('source')}, {len(report.segments)} segment(s), "
        f"{len(report.skipped_hours)} skipped hour(s)",
        f"- Fees: maker {report.run_config.get('maker_fee')}, taker {report.run_config.get('taker_fee')}; "
        f"latency {report.run_config.get('latency_ms')} ms; queue {report.run_config.get('queue_model')}",
        "",
    ]
    results = report.results
    if results is not None:
        headline = "Net P&L before funding" if report.before_funding else "Net P&L"
        lines += [
            "## Results",
            "",
            "| Measure | Value |",
            "|---|---|",
            f"| {headline} (USD) | {_money(results.net_pnl)} |",
            f"| Return after costs | {_pct(results.return_after_costs)} |",
            f"| Max drawdown | {_pct(results.max_drawdown)} |",
            f"| Win rate | {_pct(results.win_rate)} |",
            f"| Closed trades | {results.trade_count} |",
            f"| Fees (USD) | {_money(results.fees_total)} |",
            f"| Funding (USD) | {_money(results.funding_total)} |",
            f"| Positions open at data end | {results.open_positions} |",
            "",
        ]
    band = report.loser_band
    if band is not None:
        lines += [
            "## Loser check (AC-14)",
            "",
            f"- Trades used: {band.trades_used}; total net P&L {_money(band.total_net)} USD",
            f"- Expected mean per trade: {_money(band.expected, 4)} USD; "
            f"band {_money(band.low, 4)} to {_money(band.high, 4)}",
            f"- Mean net per trade: {_money(band.mean_net, 4)} USD (standard error {_money(band.std_error, 4)})",
            f"- Result: **{'pass' if band.passed else 'fail'}**",
            "",
        ]
    if report.skipped_hours:
        reasons: dict[str, int] = {}
        for _, reason in report.skipped_hours:
            reasons[reason] = reasons.get(reason, 0) + 1
        lines += ["## Skipped hours", ""] + [f"- {reason}: {count}" for reason, count in sorted(reasons.items())] + [""]
    return "\n".join(lines)


def index_markdown(batch_id: str, reports: Sequence[LabReport]) -> str:
    """Every variant of the batch side by side (failed and interrupted ones marked)."""
    lines = [
        f"# Replay batch `{batch_id}`",
        "",
        "| Variant | Status | Net P&L (USD) | Fees (USD) | Trades | Win rate | Max drawdown |",
        "|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        r = report.results
        name = report.variant.replace("=", "-")
        if r is None:
            lines.append(f"| [{report.variant}]({name}.md) | {report.status} | n/a | n/a | n/a | n/a | n/a |")
        else:
            lines.append(
                f"| [{report.variant}]({name}.md) | {report.status} | {_money(r.net_pnl)} | {_money(r.fees_total)} | "
                f"{r.trade_count} | {_pct(r.win_rate)} | {_pct(r.max_drawdown)} |"
            )
    return "\n".join(lines) + "\n"


def write_variant(folder: Path, report: LabReport, bundle: RunBundle | None) -> None:
    """Write ``<variant>.md``, ``<variant>.json``, and ``<variant>.bundle.json`` when a bundle was written."""
    folder.mkdir(parents=True, exist_ok=True)
    name = report.variant.replace("=", "-")
    (folder / f"{name}.json").write_text(report_json(report), encoding="utf-8")
    (folder / f"{name}.md").write_text(report_markdown(report), encoding="utf-8")
    if bundle is not None:
        (folder / f"{name}.bundle.json").write_text(bundle_json(bundle), encoding="utf-8")
