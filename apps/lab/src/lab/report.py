"""Reports of a replay batch (spec 0008 AC-11, AC-13).

Per variant JSON, Markdown, and the exact bundle, plus the batch's ``index.md``.

``LabReport`` is dumped with ``model_dump(mode="json")``: Decimals as strings, nanosecond times
as integers, keys sorted. The Markdown renders the same model. Nothing that differs between two
runs of the same inputs goes in, except ``run_id``, ``batch_id``, ``started_at``, and
``ended_at`` (AC-12).
"""

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading_core.recording.bundle_codec import bundle_json
from trading_core.recording.enums import Intent, RiskVerdict
from trading_core.recording.rows import JsonValue, RunBundle, ns_to_utc

Status = Literal["completed", "failed", "interrupted"]
HORIZON_KEYS = ("1s", "5s", "30s")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorInfo(_Model):
    """Why a variant failed."""

    type: str
    message: str


class FillStatsReport(_Model):
    """One liquidity side's fill rate and markouts (AC-10); horizons keyed ``1s``, ``5s``, ``30s``."""

    liquidity: str
    orders_placed: int
    orders_filled: int
    fill_count: int
    fill_rate: Decimal
    markouts_bps: dict[str, Decimal | None]
    # Fills left out of each horizon's mean because it passed their segment's last delta.
    left_out: dict[str, int]


class Results(_Model):
    """The variant's ``run_results`` numbers, its net P&L, and its fill stats per side."""

    net_pnl: Decimal
    return_after_costs: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trade_count: int
    fees_total: Decimal
    funding_total: Decimal
    # Positions still open after the segment end close (AC-8); 0 unless something went wrong.
    open_positions: int
    fill_stats: list[FillStatsReport] = []


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


class MonthQuality(_Model):
    """One month the range touches, from its manifest (whole month, not prorated to the range)."""

    month: str
    missing_minutes: int
    quiet_minutes: int
    volume_rounded_rows: int
    zero_volume_trade_rows: int
    missing_funding_charges: int


class SideSplitReport(_Model):
    """Closed trades and their net P&L on one side."""

    trades: int
    net_pnl: Decimal


class CoinReport(_Model):
    """One coin's share of a candle run (spec 0022 AC-9): its trades, costs, splits, and data quality."""

    trade_count: int
    net_pnl: Decimal
    fees_total: Decimal
    funding_total: Decimal
    long: SideSplitReport
    short: SideSplitReport
    exposure: Decimal
    benchmark: Decimal | None
    events: int
    first_trade_ns: int | None
    last_point_ns: int
    first_candle_ns: int | None
    last_candle_ns: int | None
    gaps: list[tuple[int, int, int]]
    months: list[MonthQuality]
    carried_funding_charges: int
    zero_volume_bars: int


class CandleReport(_Model):
    """What a candle backtest adds (spec 0014 AC-16): marked equity, the splits, costs, and data quality.

    The figures are over the whole run (for a multi coin run, the shared account's combined ones);
    ``per_instrument`` holds each coin's own, keys in sorted order, one key for a single coin run."""

    range: tuple[int, int]
    marked_max_drawdown: Decimal
    longest_drawdown_hours: int
    long: SideSplitReport
    short: SideSplitReport
    exposure: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    liquidations: int
    ruined: bool
    # When account equity reached zero and the variant ended early, else None (AC-14).
    ruined_ns: int | None = None
    first_decision_ns: int | None
    benchmark: Decimal | None
    # Data quality: the span with candles, the gap rows overlapping the range, and per month figures.
    first_candle_ns: int | None
    last_candle_ns: int | None
    gaps: list[tuple[int, int, int]]
    months: list[MonthQuality]
    carried_funding_charges: int
    zero_volume_bars: int
    per_instrument: dict[str, CoinReport] = {}


class EntryDecisionCount(_Model):
    """How many ``enter`` decisions one UTC day had with one outcome: ``approved`` or the rejection reason."""

    day: str
    outcome: str
    count: int


def entry_decision_counts(bundle: RunBundle | None) -> list[EntryDecisionCount]:
    """The ``enter`` decisions per UTC day and outcome (spec 0017 AC-5), so the signal rate shows apart from fills."""
    counts: dict[tuple[str, str], int] = {}
    for decision in () if bundle is None else bundle.decisions:
        if decision.intent != Intent.ENTER:
            continue
        outcome = "approved" if decision.risk_verdict == RiskVerdict.APPROVED else decision.risk_reason or "rejected"
        key = (ns_to_utc(decision.decided_ns).date().isoformat(), outcome)
        counts[key] = counts.get(key, 0) + 1
    return [EntryDecisionCount(day=day, outcome=outcome, count=n) for (day, outcome), n in sorted(counts.items())]


def _entry_decision_lines(report: LabReport) -> list[str]:
    if not report.entry_decisions:
        return []
    outcomes = sorted({c.outcome for c in report.entry_decisions}, key=lambda o: (o != "approved", o))
    days = sorted({c.day for c in report.entry_decisions})
    table = {(c.day, c.outcome): c.count for c in report.entry_decisions}
    lines = [
        "## Entry decisions",
        "",
        "| Day | " + " | ".join(outcomes) + " |",
        "|---|" + "---|" * len(outcomes),
    ]
    for day in days:
        lines.append(f"| {day} | " + " | ".join(str(table.get((day, o), 0)) for o in outcomes) + " |")
    totals = " | ".join(str(sum(table.get((d, o), 0) for d in days)) for o in outcomes)
    return [*lines, f"| all | {totals} |", ""]


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
    # An ``own`` run whose rates missed this boundary applies no funding at all (spec 0009 AC-14).
    funding_skipped_ns: int | None = None
    # Set on a candle backtest's completed variant (spec 0014).
    candle: CandleReport | None = None
    # The enter decisions per UTC day and outcome (spec 0017 AC-5).
    entry_decisions: list[EntryDecisionCount] = []


def report_json(report: LabReport) -> str:
    """The report as JSON with sorted keys."""
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _utc(ns: int) -> str:
    return ns_to_utc(ns).strftime("%Y-%m-%d %H:%M UTC")


def _money(value: Decimal | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{places}f}"


def _pct(value: Decimal) -> str:
    return f"{value * 100:.3f} %"


def _bps(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _side(results: Results, liquidity: str) -> FillStatsReport | None:
    return next((s for s in results.fill_stats if s.liquidity == liquidity), None)


def _side_cells(side: FillStatsReport | None) -> str:
    if side is None:
        return "n/a | n/a"
    return f"{_pct(side.fill_rate)} | " + " / ".join(_bps(side.markouts_bps.get(h)) for h in HORIZON_KEYS)


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
        f"latency {report.run_config.get('latency_ms')} ms; queue {report.run_config.get('queue_model')}"
        + (f"; entry {report.run_config['entry_mode']}" if report.run_config.get("entry_mode") else ""),
    ]
    if report.funding_skipped_ns is not None:
        lines.append(f"- funding skipped: no rate before {_utc(report.funding_skipped_ns)}")
    if report.run_config.get("segment_end_partial"):
        lines.append("- Segment end: the last book could not close every position; the rest closed at the last mid")
    lines.append("")
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
    if results is not None and results.fill_stats:
        lines += [
            "## Entry fills (AC-10)",
            "",
            "| Side | Placed | Filled | Fills | Fill rate | Markout 1 s (bps) | 5 s | 30 s | Left out (1/5/30 s) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for s in results.fill_stats:
            marks = " | ".join(_bps(s.markouts_bps.get(h)) for h in HORIZON_KEYS)
            left = "/".join(str(s.left_out.get(h, 0)) for h in HORIZON_KEYS)
            lines.append(
                f"| {s.liquidity} | {s.orders_placed} | {s.orders_filled} | {s.fill_count} | "
                f"{_pct(s.fill_rate)} | {marks} | {left} |"
            )
        lines.append("")
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
    lines += _entry_decision_lines(report)
    if report.skipped_hours:
        reasons: dict[str, int] = {}
        for _, reason in report.skipped_hours:
            reasons[reason] = reasons.get(reason, 0) + 1
        lines += ["## Skipped hours", ""] + [f"- {reason}: {count}" for reason, count in sorted(reasons.items())] + [""]
    return "\n".join(lines)


def candle_report_markdown(report: LabReport) -> str:
    """A candle backtest variant as a short Markdown page (spec 0014 AC-16)."""
    config = report.run_config
    lines = [f"# Backtest `{report.variant}` · {report.strategy}", "", f"- Status: **{report.status}**"]
    if report.error is not None:
        lines.append(f"- Error: `{report.error.type}`: {report.error.message}")
    lines += [
        f"- Batch: `{report.batch_id}`",
        f"- Run: `{report.run_id or 'not written'}`",
        f"- Bars: {config.get('bar_type')}; slippage {config.get('slippage_bps')} bps per side",
        f"- Fees: maker {config.get('maker_fee')}, taker {config.get('taker_fee')}",
        f"- Funding: {config.get('funding_source')}",
        "",
    ]
    results, candle = report.results, report.candle
    if results is not None and candle is not None:
        lines += [
            "## Results",
            "",
            "| Measure | Value |",
            "|---|---|",
            f"| Net P&L (USD) | {_money(results.net_pnl)} |",
            f"| Return after costs | {_pct(results.return_after_costs)} |",
            f"| Buy and hold, before costs | {'n/a' if candle.benchmark is None else _pct(candle.benchmark)} |",
            f"| Max drawdown, realized | {_pct(results.max_drawdown)} |",
            f"| Max drawdown, marked to market | {_pct(candle.marked_max_drawdown)} |",
            f"| Longest drawdown (hours) | {candle.longest_drawdown_hours} |",
            f"| Closed trades | {results.trade_count} (long {candle.long.trades}, short {candle.short.trades}) |",
            f"| Net P&L long / short (USD) | {_money(candle.long.net_pnl)} / {_money(candle.short.net_pnl)} |",
            f"| Win rate | {_pct(results.win_rate)} |",
            f"| Profit factor | {'n/a' if candle.profit_factor is None else f'{candle.profit_factor:.3f}'} |",
            f"| Expectancy per trade (USD) | {_money(candle.expectancy)} |",
            f"| Exposure time | {_pct(candle.exposure)} |",
            f"| Fees (USD) | {_money(results.fees_total)} |",
            f"| Funding (USD) | {_money(results.funding_total)} |",
            f"| Liquidations | {candle.liquidations} |",
            f"| Ruined | {'no' if candle.ruined_ns is None else f'yes, at {_utc(candle.ruined_ns)}'} |",
            f"| First decision | {'none' if candle.first_decision_ns is None else _utc(candle.first_decision_ns)} |",
            "",
            "## Data quality",
            "",
            f"- Candles from {_span(candle.first_candle_ns)} to {_span(candle.last_candle_ns)}",
            f"- Gap rows overlapping the range: {len(candle.gaps)}",
            f"- Funding charges at a carried rate: {candle.carried_funding_charges}",
            f"- Zero volume `{config.get('bar_spec')}` bars: {candle.zero_volume_bars}",
            "",
            "Per month touched (whole months, not prorated to the range):",
            "",
            "| Month | Missing minutes | Quiet minutes | Rounded volumes | Zero volume trades | Missing funding |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {m.month} | {m.missing_minutes} | {m.quiet_minutes} | {m.volume_rounded_rows} | "
            f"{m.zero_volume_trade_rows} | {m.missing_funding_charges} |"
            for m in candle.months
        ]
        lines.append("")
        if len(candle.per_instrument) > 1:
            lines += _coin_lines(candle, config)
    lines += _entry_decision_lines(report)
    return "\n".join(lines)


def _coin_lines(candle: CandleReport, config: dict[str, JsonValue]) -> list[str]:
    """Each coin of a multi coin run: its share of the results, then its own data quality (spec 0022 AC-9)."""
    coins = list(candle.per_instrument)
    lines = [
        "## Per coin",
        "",
        "| Measure | " + " | ".join(coins) + " |",
        "|---|" + "---|" * len(coins),
    ]

    def row(name: str, cell: Callable[[CoinReport], str]) -> None:
        lines.append(f"| {name} | " + " | ".join(cell(c) for c in candle.per_instrument.values()) + " |")

    row("Closed trades", lambda c: f"{c.trade_count} (long {c.long.trades}, short {c.short.trades})")
    row("Net P&L (USD)", lambda c: _money(c.net_pnl))
    row("Net P&L long / short (USD)", lambda c: f"{_money(c.long.net_pnl)} / {_money(c.short.net_pnl)}")
    row("Fees (USD)", lambda c: _money(c.fees_total))
    row("Funding (USD)", lambda c: _money(c.funding_total))
    row("Exposure time", lambda c: _pct(c.exposure))
    row("Buy and hold, before costs", lambda c: "n/a" if c.benchmark is None else _pct(c.benchmark))
    row("Candles from", lambda c: _span(c.first_candle_ns))
    row("Candles to", lambda c: _span(c.last_candle_ns))
    row("Gap rows overlapping the range", lambda c: str(len(c.gaps)))
    row("Funding charges at a carried rate", lambda c: str(c.carried_funding_charges))
    row(f"Zero volume `{config.get('bar_spec')}` bars", lambda c: str(c.zero_volume_bars))
    lines.append("")
    for coin, c in candle.per_instrument.items():
        lines += [
            f"{coin} per month touched:",
            "",
            "| Month | Missing minutes | Quiet minutes | Rounded volumes | Zero volume trades | Missing funding |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {m.month} | {m.missing_minutes} | {m.quiet_minutes} | {m.volume_rounded_rows} | "
            f"{m.zero_volume_trade_rows} | {m.missing_funding_charges} |"
            for m in c.months
        ]
        lines.append("")
    return lines


def _span(ns: int | None) -> str:
    return "n/a" if ns is None else _utc(ns)


def _coins(config: dict[str, JsonValue]) -> str:
    """The run's coins by symbol, ``PF_ETHUSD + PF_XBTUSD`` for a multi coin run (spec 0022 AC-9)."""
    ids = config.get("instrument_ids")
    names = [str(i) for i in ids] if isinstance(ids, list) else [str(config.get("instrument_id"))]
    return " + ".join(n.split(".")[0] for n in names)


def candle_index_markdown(batch_id: str, reports: Sequence[LabReport]) -> str:
    """Every candle variant side by side (spec 0014 AC-16)."""
    lines = [
        f"# Backtest batch `{batch_id}`",
        "",
        "| Variant | Status | Coins | Net P&L (USD) | Return | Max DD realized | Max DD marked | Fees (USD) | "
        "Funding (USD) | Trades | Exposure | Liquidations | First decision | Buy and hold |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        r, c = report.results, report.candle
        name = report.variant.replace("=", "-")
        coins = _coins(report.run_config)
        if r is None or c is None:
            lines.append(f"| [{report.variant}]({name}.md) | {report.status} | {coins} |" + " n/a |" * 11)
            continue
        first = "none" if c.first_decision_ns is None else _utc(c.first_decision_ns)
        bench = "n/a" if c.benchmark is None else _pct(c.benchmark)
        lines.append(
            f"| [{report.variant}]({name}.md) | {report.status} | {coins} | {_money(r.net_pnl)} | "
            f"{_pct(r.return_after_costs)} | "
            f"{_pct(r.max_drawdown)} | {_pct(c.marked_max_drawdown)} | {_money(r.fees_total)} | "
            f"{_money(r.funding_total)} | {r.trade_count} | {_pct(c.exposure)} | {c.liquidations} | {first} | {bench} |"
        )
    return "\n".join(lines) + "\n"


def index_markdown(batch_id: str, reports: Sequence[LabReport]) -> str:
    """Every variant of the batch side by side (failed and interrupted ones marked): net P&L, fees,
    trades, and each side's fill rate and markouts at 1, 5, and 30 s (AC-18)."""
    lines = [
        f"# Replay batch `{batch_id}`",
        "",
        "Markouts are in bps at 1 s / 5 s / 30 s after each entry fill, weighted by fill size.",
        "",
        "| Variant | Status | Net P&L (USD) | Fees (USD) | Trades | Maker fill rate | Maker markouts | "
        "Taker fill rate | Taker markouts |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        r = report.results
        name = report.variant.replace("=", "-")
        if r is None:
            lines.append(
                f"| [{report.variant}]({name}.md) | {report.status} | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
            )
        else:
            lines.append(
                f"| [{report.variant}]({name}.md) | {report.status} | {_money(r.net_pnl)} | {_money(r.fees_total)} | "
                f"{r.trade_count} | {_side_cells(_side(r, 'maker'))} | {_side_cells(_side(r, 'taker'))} |"
            )
    return "\n".join(lines) + "\n"


def write_variant(folder: Path, report: LabReport, bundle: RunBundle | None) -> None:
    """Write ``<variant>.md``, ``<variant>.json``, and ``<variant>.bundle.json`` when a bundle was written."""
    folder.mkdir(parents=True, exist_ok=True)
    name = report.variant.replace("=", "-")
    (folder / f"{name}.json").write_text(report_json(report), encoding="utf-8")
    candle = report.run_config.get("source") == "candles"
    markdown = candle_report_markdown(report) if candle else report_markdown(report)
    (folder / f"{name}.md").write_text(markdown, encoding="utf-8")
    if bundle is not None:
        (folder / f"{name}.bundle.json").write_text(bundle_json(bundle), encoding="utf-8")
