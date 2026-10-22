"""``lab screen <study>`` (spec 0017 AC-9): the candle screen's verdict on a scalping candidate.

Read only as ``lab_app``. It refuses (64) anything but a candle study of a scalping strategy whose every
grid point has a completed development run, picks the final point with the rule ``lab holdout`` uses,
applies the pure ``trading_core.lab.screen`` rule, prints the verdict, and writes
``lab-reports/screen-<study>/screen.json``. Exit 0 on a pass, 3 on a fail. A candle gate study
(AC-20) has the lower trade rate floor ``CANDLE_GATE_MIN_TRADES_PER_DAY``.
"""

import json
import logging
import sys
from collections.abc import Sequence
from typing import Final

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lab.settings import LabSettings
from trading_core.lab.screen import (
    CANDLE_GATE_MIN_TRADES_PER_DAY,
    SCREEN_MIN_TRADES_PER_DAY,
    is_candle_gate,
    screen_figures,
    screen_verdict,
)
from trading_core.recording import repository
from trading_core.recording.enums import StrategyKind, StudyLab
from trading_core.recording.gate_queries import UnknownStudyError, study_evidence

log = logging.getLogger(__name__)

EXIT_PASS: Final = 0
EXIT_FAIL: Final = 3
EXIT_USAGE: Final = 64


def _refuse(problems: Sequence[str]) -> int:
    for problem in problems:
        print(f"lab screen: {problem}", file=sys.stderr)
    log.error("lab_bad_input", extra={"fields": {"problems": list(problems)}})
    return EXIT_USAGE


def _text(value: object) -> object:
    return None if value is None else str(value)


def run_screen(settings: LabSettings, name: str, engine: Engine) -> int:
    """Screen the study called ``name`` (see the module docstring)."""
    with Session(engine) as session:
        try:
            evidence = study_evidence(session, name)
        except UnknownStudyError:
            return _refuse([f"no study called {name!r}"])
        study, inputs = evidence.study, evidence.inputs
        if study.lab != StudyLab.CANDLE or inputs.strategy_kind != StrategyKind.SCALPING:
            return _refuse([f"{name!r} is not a candle study of a scalping strategy"])
        grid = study.plan.get("grid")
        floor = CANDLE_GATE_MIN_TRADES_PER_DAY if is_candle_gate(study.plan) else SCREEN_MIN_TRADES_PER_DAY
        points = len(grid) if isinstance(grid, list) and grid else 1
        done = repository.completed_grid_points(session, study.id)
        missing = [str(i) for i in range(points) if i not in done]
        if missing:
            return _refuse([f"grid points without a completed development run: {', '.join(missing)}"])
        final = repository.final_grid_point(session, study.id)
        session.rollback()
    assert final is not None  # every point completed
    figures = screen_figures(inputs, final)
    verdict = screen_verdict(figures, inputs.thresholds.fee_hurdle_k, floor)
    report = {
        "study": name,
        "strategy": inputs.strategy_name,
        "verdict": "pass" if verdict.passed else "fail",
        "reasons": list(verdict.reasons),
        "trade_rate_only": verdict.trade_rate_only,
        "final_grid_point": final,
        "figures": {
            "walk_forward_sharpe": _text(figures.walk_forward_sharpe),
            "avg_gross_edge": _text(figures.avg_gross_edge),
            "avg_round_trip_cost": _text(figures.avg_round_trip_cost),
            "fee_hurdle_k": str(inputs.thresholds.fee_hurdle_k),
            "trade_count": figures.trade_count,
            "days": figures.days,
            "trades_per_day": _text(figures.trades_per_day),
            "min_trades_per_day": floor if isinstance(floor, int) else str(floor),
        },
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    folder = settings.lab_reports_dir / f"screen-{name}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "screen.json").write_text(text, encoding="utf-8")
    print(text, end="")
    log.info("lab_screened", extra={"fields": {"study": name, "verdict": report["verdict"]}})
    return EXIT_PASS if verdict.passed else EXIT_FAIL
