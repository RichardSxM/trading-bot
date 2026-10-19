"""The candidate → paper verdict (spec 0015 AC-10, AC-12): run every check, then fail, insufficient, or pass.

The verdict is ``fail`` if any reason is a missed threshold, else ``insufficient`` if any reason is
insufficient evidence, else ``pass``. Missing evidence never passes: no holdout, no benchmark, a
missing cost, or a NULL code hash each give an insufficient reason.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from trading_core.gate.checks import (
    BENCHMARKS,
    FAILED,
    FOLD_WITHOUT_RUN,
    MISSING_COST,
    MISSING_METRIC,
    MISSING_RUN,
    NULL_CODE_HASH,
    TOO_FEW_DAYS,
    ZERO_VARIANCE,
    MetricValue,
    Outcome,
    Reason,
    check_beats_benchmark,
    check_concentration,
    check_concentration_folds,
    check_drawdown,
    check_fee_hurdle,
    check_holdout_sharpe,
    check_trade_floor,
    text_of,
)
from trading_core.gate.folds import FoldRange, candle_ranges, common_days, replay_ranges, walk_forward
from trading_core.gate.inputs import CandidateInputs, Lab, Role, StudyRunInput
from trading_core.gate.series import DailySeries, annualized, sample_std, sharpe, to_decimal
from trading_core.gate.stats import deflated_sharpe, moments, pbo_cscv, politis_white_block, stationary_bootstrap_lower


class GateVerdict(StrEnum):
    """Mirrors ``recording.enums.Verdict``."""

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class FoldResult:
    """One scored walk forward fold, as ``promotion_folds`` stores it."""

    fold: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    chosen_grid_point: int
    is_sharpe: str | None
    oos_sharpe: str | None
    oos_return: str
    oos_trades: int
    oos_net_pnl: str


@dataclass(frozen=True)
class GateResult:
    """What an evaluation writes: the verdict, its reasons, its metrics, and its folds."""

    verdict: GateVerdict
    reasons: tuple[Reason, ...]
    metrics: dict[str, MetricValue]
    folds: tuple[FoldResult, ...] = field(default=())


def decide(reasons: Sequence[Reason]) -> GateVerdict:
    """``fail`` before ``insufficient`` before ``pass`` (AC-12)."""
    if any(r.fails for r in reasons):
        return GateVerdict.FAIL
    if reasons:
        return GateVerdict.INSUFFICIENT
    return GateVerdict.PASS


def _cost_reasons(run: StudyRunInput, out: Outcome) -> None:
    """A missing cost on the holdout makes its numbers untrustworthy (AC-12)."""
    assert run.summary is not None
    if run.summary.trade_count > 0 and run.summary.fees_total == 0:
        out.reason(MISSING_COST, "costs", "fees_total=0")
    if run.funding_missing:
        out.reason(MISSING_COST, "costs", "funding")
    if run.cost_setting_missing:
        out.reason(MISSING_COST, "costs", "slippage or latency setting")


def _ratio(value: float | None) -> Decimal | None:
    return to_decimal(annualized(value))


def _development_points(inputs: CandidateInputs) -> dict[int, DailySeries]:
    """Each completed grid point's development series, keyed by grid index (the first run of a point)."""
    points: dict[int, DailySeries] = {}
    for run in inputs.by_role(Role.DEVELOPMENT):
        if run.completed and run.grid_point is not None and run.grid_point not in points:
            points[run.grid_point] = run.series(inputs.lab)
    return points


def _fold_ranges(inputs: CandidateInputs, points: dict[int, DailySeries]) -> list[FoldRange]:
    layout = dict(inputs.fold_overrides)
    t = inputs.thresholds
    if inputs.lab == Lab.CANDLE:
        days = sorted({d for s in points.values() for d in s.days})
        if not days:
            return []
        return candle_ranges(
            days[0],
            days[-1],
            layout.get("first_in_sample_months", t.candle.first_in_sample_months),
            layout.get("step_months", t.candle.step_months),
        )
    return replay_ranges(
        common_days(points),
        layout.get("first_in_sample_days", t.replay.first_in_sample_days),
        layout.get("step_days", t.replay.step_days),
    )


def _walk_forward(inputs: CandidateInputs, points: dict[int, DailySeries], out: Outcome) -> list[FoldResult]:
    """``walk_forward_sharpe`` (AC-9): fails at or below 0; insufficient without folds or with a fold lacking a run."""
    ranges = _fold_ranges(inputs, points)
    wf = walk_forward(ranges, points) if ranges else None
    folds = [] if wf is None else list(wf.folds)
    value = None if wf is None else _ratio(sharpe(wf.series.returns))
    out.metric("walk_forward_sharpe", value)
    out.metric("walk_forward_return", None if wf is None or not len(wf.series) else to_decimal(wf.series.compounded()))
    out.metric("walk_forward_folds", len(folds))
    if wf is None:
        out.reason(TOO_FEW_DAYS, "walk_forward_sharpe", 0, 1)
    elif wf.missing:
        out.reason(FOLD_WITHOUT_RUN, "walk_forward_sharpe", ",".join(map(str, wf.missing)))
    elif value is None:
        out.reason(ZERO_VARIANCE, "walk_forward_sharpe")
    elif value <= 0:
        out.reason(FAILED, "walk_forward_sharpe", value, 0)
    nets = [f.oos.total_net() for f in folds]
    check_concentration_folds(inputs, nets, out)
    return [
        FoldResult(
            fold=f.fold,
            is_start=f.is_start.isoformat(),
            is_end=f.is_end.isoformat(),
            oos_start=f.oos_start.isoformat(),
            oos_end=f.oos_end.isoformat(),
            chosen_grid_point=f.chosen_grid_point,
            is_sharpe=text_of(_ratio(f.is_sharpe)),
            oos_sharpe=text_of(_ratio(sharpe(f.oos.returns))),
            oos_return=format(to_decimal(f.oos.compounded()) or Decimal(0), "f"),
            oos_trades=f.oos.total_trades(),
            oos_net_pnl=format(f.oos.total_net(), "f"),
        )
        for f in folds
    ]


def _block(inputs: CandidateInputs, values: list[float]) -> int:
    """Politis and White (at least ``bootstrap_min_block_days``) for candle; 1 for replay (independent days)."""
    if inputs.lab == Lab.REPLAY:
        return 1
    return politis_white_block(values, inputs.thresholds.bootstrap_min_block_days)


def _bootstrap(inputs: CandidateInputs, series: DailySeries, out: Outcome) -> None:
    """``bootstrap_lower_bound`` on mean holdout daily return, and the trade return bound for paper → live."""
    t = inputs.thresholds
    confidence, resamples = float(t.bootstrap_confidence), t.bootstrap_resamples
    days = list(series.returns)
    if len(days) < 10:
        out.metric("bootstrap_lower_bound", None)
        out.reason(TOO_FEW_DAYS, "bootstrap_lower_bound", len(days), 10)
    else:
        bound = stationary_bootstrap_lower(days, _block(inputs, days), resamples, confidence, inputs.seed)
        lower = to_decimal(bound.lower)
        out.metric("bootstrap_lower_bound", lower)
        out.metric("bootstrap_block_days", bound.block)
        if lower is not None and lower <= 0:
            out.reason(FAILED, "bootstrap_lower_bound", lower, 0)
    trades = [float(tr.ret) for tr in inputs.holdout_trades]
    if trades:
        bound = stationary_bootstrap_lower(trades, _block(inputs, trades), resamples, confidence, inputs.seed)
        out.metric("trade_return_lower_bound", to_decimal(bound.lower))
    else:
        out.metric("trade_return_lower_bound", None)


def _deflated(inputs: CandidateInputs, holdout: StudyRunInput, points: dict[int, DailySeries], out: Outcome) -> None:
    """``deflated_sharpe``: the final version's development Sharpe deflated by every trial of the strategy."""
    out.metric("trial_count", inputs.trial_count)
    series = None if holdout.grid_point is None else points.get(holdout.grid_point)
    sr = None if series is None else sharpe(series.returns)
    if series is None or sr is None:
        out.metric("deflated_sharpe", None)
        out.reason(MISSING_METRIC, "deflated_sharpe")
        return
    skew, kurt = moments(series.returns)
    trial_sharpes = [x for x in (sharpe(s.returns) for s in points.values()) if x is not None]
    variance = sample_std(trial_sharpes) ** 2 if len(trial_sharpes) >= 2 else 0.0
    value = deflated_sharpe(sr, len(series), skew, kurt, inputs.trial_count, variance)
    dsr = to_decimal(value)
    out.metric("deflated_sharpe", dsr)
    if dsr is None:
        out.reason(MISSING_METRIC, "deflated_sharpe")
    elif dsr < inputs.thresholds.dsr_min:
        out.reason(FAILED, "deflated_sharpe", dsr, inputs.thresholds.dsr_min)


def _pbo(inputs: CandidateInputs, points: dict[int, DailySeries], out: Outcome) -> None:
    """``pbo``: over 0.30 fails; with fewer than 4 completed grid points it does not apply (and passes)."""
    if len(points) < 4:
        out.metric("pbo", None)
        out.metric("pbo_applicable", False)
        return
    out.metric("pbo_applicable", True)
    days = common_days(points)
    order = sorted(points)
    columns = [points[p].only(days) for p in order]
    blocks = inputs.thresholds.cscv_blocks
    if len(days) < blocks:
        out.metric("pbo", None)
        out.reason(TOO_FEW_DAYS, "pbo", len(days), blocks)
        return
    matrix = [[columns[j].returns[i] for j in range(len(order))] for i in range(len(days))]
    value = to_decimal(pbo_cscv(matrix, blocks))
    out.metric("pbo", value)
    if value is not None and value > inputs.thresholds.pbo_max:
        out.reason(FAILED, "pbo", value, inputs.thresholds.pbo_max)


def evaluate_candidate(inputs: CandidateInputs) -> GateResult:
    """Score a study's runs and decide candidate → paper; deterministic for the same inputs."""
    out = Outcome()
    out.metric("lab", inputs.lab.value)
    if inputs.code_hash is None:
        out.reason(NULL_CODE_HASH, "frozen_version")
    if not any(r.completed for r in inputs.by_role(Role.DEVELOPMENT)):
        out.reason(MISSING_RUN, "development")
    holdout = inputs.holdout()
    if holdout is None:
        out.reason(MISSING_RUN, "holdout")
        return GateResult(decide(out.reasons), tuple(out.reasons), out.metrics)

    assert holdout.summary is not None
    series = holdout.series(inputs.lab)
    out.metric("holdout_run_id", str(holdout.run_id))
    out.metric("holdout_return", holdout.summary.return_after_costs)
    out.metric("holdout_win_rate", holdout.summary.win_rate)
    out.metric("holdout_days", len(series))
    _cost_reasons(holdout, out)
    check_trade_floor(inputs, holdout, out)
    check_fee_hurdle(inputs, holdout, out)
    holdout_sharpe = check_holdout_sharpe(series, out)
    for strategy in BENCHMARKS.get(inputs.strategy_kind, BENCHMARKS["scalping"]):
        check_beats_benchmark(inputs, series, holdout_sharpe, strategy, out)
    points = _development_points(inputs)
    folds = _walk_forward(inputs, points, out)
    _bootstrap(inputs, series, out)
    _deflated(inputs, holdout, points, out)
    _pbo(inputs, points, out)
    check_drawdown(inputs, holdout, out)
    check_concentration(inputs, holdout, out)
    return GateResult(decide(out.reasons), tuple(out.reasons), out.metrics, tuple(folds))
