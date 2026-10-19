"""The Strategies page (spec 0015 AC-18): the ranking's column heads and formatted rows.

Every number comes from the study's latest ``candidate → paper`` promotion. Win rate is shown, never a
sort key (the page has no sorting at all; the order is the gate's ranking).
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from markupsafe import Markup

from dashboard.data.strategies import StrategiesView
from dashboard.ui.format import num, ts
from dashboard.web.trades import Cell, PagerView
from trading_core.recording.gate_queries import (
    LOG_PAGE_SIZE,
    PaperProgress,
    PromotionLine,
    StudyDetail,
    StudyRanking,
)
from trading_core.recording.models import PromotionFold

DASH: Final = "—"
# (heading, is_number), in AC-18's order after the study's own name.
RANKING_COLUMNS: Final = [
    ("Study", False),
    ("Strategy", False),
    ("Lab", False),
    ("Entry", False),
    ("Stage", False),
    ("Verdict", False),
    ("Walk forward Sharpe", True),
    ("Holdout Sharpe vs buy and hold", True),
    ("Holdout return", True),
    ("Holdout max drawdown", True),
    ("Deflated Sharpe", True),
    ("PBO", True),
    ("Holdout trades", True),
    ("Win rate", True),
]
_HUNDREDTH: Final = Decimal("0.01")


def _ratio(value: Decimal | None) -> str:
    return DASH if value is None else num(value.quantize(_HUNDREDTH))


def _pct(value: Decimal | None) -> str:
    return DASH if value is None else f"{num((value * 100).quantize(_HUNDREDTH))} %"


def ranking_cells(row: StudyRanking, *, link: bool = True) -> list[Cell]:
    """One table row under ``RANKING_COLUMNS``, the study linked to its detail; a missing metric shows as a dash."""
    trades = row.integer("holdout_trades")
    drawdown = row.decimal("holdout_max_drawdown")
    basis = row.metrics.get("drawdown_basis")
    holdout, bench = row.decimal("holdout_sharpe"), row.decimal("buy_and_hold_sharpe")
    versus = DASH if holdout is None else f"{_ratio(holdout)} vs {_ratio(bench)}"
    study: Cell = Markup('<a href="/strategies/{}">{}</a>').format(row.study_id, row.study) if link else row.study
    return [
        study,
        row.strategy,
        row.lab,
        row.entry_mode or DASH,
        row.stage,
        row.verdict or DASH,
        _ratio(row.decimal("walk_forward_sharpe")),
        versus,
        _pct(row.decimal("holdout_return")),
        DASH if drawdown is None else f"{_pct(drawdown)} {basis}" if isinstance(basis, str) else _pct(drawdown),
        _ratio(row.decimal("deflated_sharpe")),
        _ratio(row.decimal("pbo")),
        DASH if trades is None else num(Decimal(trades)),
        _pct(row.decimal("holdout_win_rate")),
    ]


def strategies_context(view: StrategiesView) -> dict[str, object]:
    """Everything ``pages/strategies.html`` renders: the ranking (each study linked to its detail) and paper
    progress."""
    return {
        "ranking_columns": RANKING_COLUMNS,
        "ranking": [ranking_cells(r) for r in view.ranking],
        "paper_columns": PAPER_COLUMNS,
        "paper": [paper_cells(p) for p in view.paper],
    }


# The study detail, the promotion log, and paper progress (AC-19 to AC-21) ----------------------

FOLD_COLUMNS: Final = [
    ("Fold", True),
    ("In sample", False),
    ("Out of sample", False),
    ("Chosen grid point", True),
    ("In sample Sharpe", True),
    ("Out of sample Sharpe", True),
    ("Out of sample return", True),
    ("Trades", True),
]
ATTEMPT_COLUMNS: Final = [("Decided", False), ("Verdict", False), ("Reasons", False)]
LOG_COLUMNS: Final = [
    ("Decided", False),
    ("Strategy", False),
    ("Version", False),
    ("Frozen hash", False),
    ("Step", False),
    ("Verdict", False),
    ("Decided by", False),
    ("Reasons", False),
    ("Note", False),
]
PAPER_COLUMNS: Final = [
    ("Strategy", False),
    ("Frozen hash", False),
    ("Paper days", True),
    ("Trades", True),
    ("Return", True),
    ("Max drawdown", True),
    ("Mean trade vs lab bound", True),
    ("Fill rate drift", True),
    ("Slippage paper vs lab (bps)", True),
]
# (label, metric, is a fraction to show as percent)
DETAIL_FIGURES: Final = [
    ("Bootstrap lower bound, mean daily return", "bootstrap_lower_bound", True),
    ("Bootstrap block (days)", "bootstrap_block_days", False),
    ("Trade return lower bound", "trade_return_lower_bound", True),
    ("Buy and hold Sharpe", "buy_and_hold_sharpe", False),
    ("MACD baseline Sharpe", "macd_baseline_sharpe", False),
    ("Largest fold share of profit", "fold_share", True),
    ("Long or short share of profit", "direction_share", True),
    ("Top trades share of profit", "top_trades_share", True),
    ("Average gross edge per trade (USD)", "avg_gross_edge", False),
    ("Average round trip cost (USD)", "avg_round_trip_cost", False),
]


def _hash(value: str | None) -> str:
    return DASH if value is None else value[:12]


def _reasons(reasons: Sequence[Mapping[str, object]]) -> str:
    return ", ".join(f"{r.get('check')}: {r.get('code')}" for r in reasons) or "none"


def fold_cells(fold: PromotionFold) -> list[Cell]:
    """One fold row under ``FOLD_COLUMNS``."""
    return [
        str(fold.fold),
        f"{fold.is_start} to {fold.is_end}",
        f"{fold.oos_start} to {fold.oos_end}",
        str(fold.chosen_grid_point),
        _ratio(fold.is_sharpe),
        _ratio(fold.oos_sharpe),
        _pct(fold.oos_return),
        num(Decimal(fold.oos_trades)),
    ]


def detail_context(detail: StudyDetail) -> dict[str, object]:
    """Everything ``pages/strategy_detail.html`` renders."""
    row = detail.ranking
    figures = []
    for label, name, fraction in DETAIL_FIGURES:
        value = row.decimal(name)
        count = row.integer(name)
        if count is not None:
            shown = num(Decimal(count))
        elif fraction:
            shown = _pct(value)
        else:
            shown = _ratio(value)
        figures.append((label, shown))
    return {
        "detail_title": row.study,
        "ranking_columns": RANKING_COLUMNS,
        "ranking": [ranking_cells(row, link=False)],
        "fold_columns": FOLD_COLUMNS,
        "folds": [fold_cells(f) for f in detail.folds],
        "figures": figures,
        "trials": detail.trials,
        "attempt_columns": ATTEMPT_COLUMNS,
        "attempts": [[ts(a.decided_at), a.verdict, _reasons(a.reasons)] for a in detail.attempts],
    }


def log_context(lines: Sequence[PromotionLine], page: int) -> dict[str, object]:
    """Everything ``pages/promotion_log.html`` renders."""
    rows: list[list[Cell]] = [
        [
            ts(line.decided_at),
            line.strategy,
            str(line.version_id),
            _hash(line.frozen_hash),
            line.step,
            line.verdict,
            line.decided_by,
            ", ".join(line.reason_codes) or DASH,
            line.notes or DASH,
        ]
        for line in lines
    ]
    shown = len(lines)
    pager_view = PagerView(
        page,
        newer=f"?page={page - 1}" if page > 1 and shown else None,
        older=f"?page={page + 1}" if shown >= LOG_PAGE_SIZE else None,
        first="?page=1" if shown == 0 and page > 1 else None,
    )
    return {"log_columns": LOG_COLUMNS, "log": rows, "log_pager": pager_view}


def paper_cells(progress: PaperProgress) -> list[Cell]:
    """One row under ``PAPER_COLUMNS``: each figure against its floor, bound, or the lab."""
    m = progress.metrics

    def dec(name: str) -> Decimal | None:
        value = m.get(name)
        return Decimal(value) if isinstance(value, str) else None

    trades = m.get("paper_trades")
    mean, bound = dec("paper_mean_trade_return"), dec("holdout_trade_return_lower_bound")
    paper_slip, lab_slip = dec("paper_slippage_bps"), dec("lab_slippage_bps")
    return [
        progress.strategy,
        _hash(progress.frozen_hash),
        DASH if dec("paper_days") is None else num(dec("paper_days")),
        DASH if not isinstance(trades, int) else num(Decimal(trades)),
        _pct(dec("paper_return")),
        _pct(dec("paper_max_drawdown")),
        f"{_pct(mean)} vs {_pct(bound)}",
        _pct(dec("fill_rate_drift")),
        f"{_ratio(paper_slip)} vs {_ratio(lab_slip)}",
    ]
