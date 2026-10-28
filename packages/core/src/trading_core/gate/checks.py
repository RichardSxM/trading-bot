"""The gate's checks (spec 0015 *Gate checks*), pure: each one gives its reasons and its metrics.

A reason is ``{code, check, value, threshold}``. ``code`` is ``failed`` when the check's threshold was
missed, and otherwise names why the evidence is insufficient (``too_few_trades``, ``missing_benchmark``,
...). A passing check gives no reason, only metrics.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from trading_core.gate.inputs import (
    BUY_AND_HOLD,
    MACD_BASELINE,
    CandidateInputs,
    Lab,
    Role,
    RunSummary,
    StudyRunInput,
)
from trading_core.gate.series import DailySeries, annualized, sharpe, to_decimal

FAILED: Final = "failed"
# Insufficient evidence codes.
TOO_FEW_TRADES: Final = "too_few_trades"
TOO_FEW_DAYS: Final = "too_few_days"
ZERO_VARIANCE: Final = "zero_variance"
MISSING_RUN: Final = "missing_run"
MISSING_BENCHMARK: Final = "missing_benchmark"
MISSING_COST: Final = "missing_cost"
MISSING_METRIC: Final = "missing_metric"
NULL_CODE_HASH: Final = "null_code_hash"
FOLD_WITHOUT_RUN: Final = "fold_without_run"
INCONSISTENT_EVIDENCE: Final = "inconsistent_evidence"

type MetricValue = str | int | bool | list[MetricValue] | dict[str, MetricValue] | None


@dataclass(frozen=True)
class Reason:
    """Why a check did not pass."""

    code: str
    check: str
    value: str | None = None
    threshold: str | None = None

    @property
    def fails(self) -> bool:
        """True for a missed threshold, False for insufficient evidence."""
        return self.code == FAILED

    def to_json(self) -> dict[str, str | None]:
        """The stored form."""
        return {"code": self.code, "check": self.check, "value": self.value, "threshold": self.threshold}


@dataclass
class Outcome:
    """Reasons and metrics collected while the checks run."""

    reasons: list[Reason] = field(default_factory=list)
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    def reason(self, code: str, check: str, value: object = None, threshold: object = None) -> None:
        """Add a reason; values are stored as strings (fixed notation for a Decimal)."""
        self.reasons.append(Reason(code, check, text_of(value), text_of(threshold)))

    def metric(self, name: str, value: Decimal | int | str | bool | None) -> None:
        """Record a metric (Decimals as fixed notation strings)."""
        self.metrics[name] = text_of(value) if isinstance(value, Decimal) else value


class GateInputError(ValueError):
    """A check was handed inputs its caller promised to rule out (a gate bug, never a verdict)."""


def summary_of(run: StudyRunInput) -> RunSummary:
    """The run's summary; raises ``GateInputError`` when it has none (a check runs only on a completed run)."""
    if run.summary is None:
        raise GateInputError(f"run {run.run_id} has no summary")
    return run.summary


def text_of(value: object) -> str | None:
    """A reason or metric value as stored: None stays None, a Decimal in fixed notation, else ``str``."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def sharpe_metric(series: DailySeries) -> Decimal | None:
    """The annualized daily Sharpe of a series as stored, or None."""
    return to_decimal(annualized(sharpe(series.returns)))


def check_versions(inputs: CandidateInputs, out: Outcome) -> None:
    """``frozen_version``: every run's ``study_runs`` frozen hash must be its own version's."""
    for run in inputs.runs:
        if run.version_frozen_hash != run.frozen_hash:
            out.reason(INCONSISTENT_EVIDENCE, "frozen_version", run.run_id, run.version_frozen_hash)


def check_holdout_evidence(inputs: CandidateInputs, holdout: StudyRunInput, out: Outcome) -> None:
    """``holdout_grid_point`` and ``holdout_days``: the holdout ran the development winner, on unseen days.

    The holdout must be the lab's final grid point, every development day must fall before
    ``holdout_start``, and every holdout day on or after it.
    """
    if holdout.grid_point != inputs.final_grid_point:
        out.reason(INCONSISTENT_EVIDENCE, "holdout_grid_point", holdout.grid_point, inputs.final_grid_point)
    start = inputs.thresholds.holdout_start
    late = [d.day for r in inputs.by_role(Role.DEVELOPMENT) for d in r.daily if d.day >= start]
    if late:
        out.reason(INCONSISTENT_EVIDENCE, "development_days", max(late), start)
    early = [d.day for d in holdout.daily if d.day < start]
    if early:
        out.reason(INCONSISTENT_EVIDENCE, "holdout_days", min(early), start)


def check_trade_floor(inputs: CandidateInputs, holdout: StudyRunInput, out: Outcome) -> None:
    """``trade_floor``: below the lab's floor of holdout trades is insufficient, never a pass."""
    t = inputs.thresholds
    floor = t.candle.min_holdout_trades if inputs.lab == Lab.CANDLE else t.replay.min_holdout_trades
    trades = summary_of(holdout).trade_count
    out.metric("holdout_trades", trades)
    if trades < floor:
        out.reason(TOO_FEW_TRADES, "trade_floor", trades, floor)


def check_holdout_sharpe(series: DailySeries, out: Outcome) -> Decimal | None:
    """``holdout_sharpe``: fails at or below 0; insufficient under 2 days or with zero variance."""
    value = sharpe_metric(series)
    out.metric("holdout_sharpe", value)
    if len(series) < 2:
        out.reason(TOO_FEW_DAYS, "holdout_sharpe", len(series), 2)
    elif value is None:
        out.reason(ZERO_VARIANCE, "holdout_sharpe")
    elif value <= 0:
        out.reason(FAILED, "holdout_sharpe", value, 0)
    return value


def check_beats_benchmark(
    inputs: CandidateInputs, series: DailySeries, holdout_sharpe: Decimal | None, strategy: str, out: Outcome
) -> None:
    """``beats_buy_and_hold`` or ``beats_macd_baseline``: the holdout Sharpe must beat the benchmark's, same days.

    A study of the benchmark's own strategy can't be judged against itself: the check does not apply
    and passes, recorded like ``pbo`` (``<check>_applicable`` false, the benchmark Sharpe null, no reason).
    """
    check = "beats_buy_and_hold" if strategy == BUY_AND_HOLD else "beats_macd_baseline"
    applicable = strategy in benchmark_names(inputs.strategy_kind, inputs.strategy_name)
    out.metric(f"{check}_applicable", applicable)
    if not applicable:
        out.metric(f"{strategy}_sharpe", None)
        return
    benchmark = inputs.benchmark(strategy)
    if benchmark is None:
        out.metric(f"{strategy}_sharpe", None)
        out.reason(MISSING_BENCHMARK, check)
        return
    bench = sharpe_metric(benchmark.series(Lab.CANDLE).only(series.days))
    out.metric(f"{strategy}_sharpe", bench)
    if holdout_sharpe is None or bench is None:
        out.reason(MISSING_METRIC, check, holdout_sharpe, bench)
    elif holdout_sharpe <= bench:
        out.reason(FAILED, check, holdout_sharpe, bench)


BENCHMARKS: Final[Mapping[str, tuple[str, ...]]] = {
    "scalping": (BUY_AND_HOLD,),
    "hour_level": (BUY_AND_HOLD, MACD_BASELINE),
}


def benchmark_names(kind: str, studied: str) -> tuple[str, ...]:
    """The benchmarks a study runs and is judged against (AC-4): its kind's list minus the studied strategy.

    Matched by strategy name on purpose, whatever the study's config. ``lab holdout`` and the gate
    both use this one rule, so they can't drift apart.
    """
    return tuple(name for name in BENCHMARKS.get(kind, BENCHMARKS["scalping"]) if name != studied)


# Slice 2: the rest of the candidate gate ------------------------------------------------------


def check_fee_hurdle(inputs: CandidateInputs, holdout: StudyRunInput, out: Outcome) -> None:
    """``fee_hurdle``: the average gross edge per trade must be at least ``fee_hurdle_k`` times the round trip cost."""
    measures = holdout.measures
    edge = None if measures is None else measures.avg_gross_edge
    cost = None if measures is None else measures.avg_round_trip_cost
    out.metric("avg_gross_edge", edge)
    out.metric("avg_round_trip_cost", cost)
    if edge is None or cost is None:
        out.reason(MISSING_COST, "fee_hurdle")
        return
    hurdle = inputs.thresholds.fee_hurdle_k * cost
    if edge < hurdle:
        out.reason(FAILED, "fee_hurdle", edge, hurdle)


def check_drawdown(inputs: CandidateInputs, holdout: StudyRunInput, out: Outcome) -> None:
    """``drawdown_budget``: marked max drawdown (candle) or realized (replay) over the budget fails."""
    summary = summary_of(holdout)
    if inputs.lab == Lab.CANDLE:
        basis = "marked"
        value = None if holdout.measures is None else holdout.measures.marked_max_drawdown
    else:
        basis, value = "realized", summary.max_drawdown
    out.metric("holdout_max_drawdown", value)
    out.metric("drawdown_basis", basis)
    budget = inputs.thresholds.max_drawdown
    if value is None:
        out.reason(MISSING_METRIC, "drawdown_budget", None, budget)
    elif value > budget:
        out.reason(FAILED, "drawdown_budget", value, budget)


def _share_check(check: str, part: Decimal, total: Decimal, limit: Decimal, metric: str, out: Outcome) -> None:
    """A concentration check: skipped when the total is not a profit (another check fails it)."""
    if total <= 0:
        out.metric(metric, None)
        return
    share = part / total
    out.metric(metric, share.quantize(Decimal("0.000001")))
    if share > limit:
        out.reason(FAILED, check, share.quantize(Decimal("0.000001")), limit)


def check_concentration_folds(inputs: CandidateInputs, fold_nets: list[Decimal], out: Outcome) -> None:
    """``concentration_fold``: the largest fold's share of walk forward net profit."""
    if fold_nets:
        limit = inputs.thresholds.concentration_max_share
        _share_check("concentration_fold", max(fold_nets), sum(fold_nets, Decimal(0)), limit, "fold_share", out)


def check_concentration(inputs: CandidateInputs, holdout: StudyRunInput, out: Outcome) -> None:
    """``concentration_direction``, ``concentration_top_trades`` (AC-10), and ``concentration_instrument``
    (spec 0022 AC-13) over the holdout."""
    limit = inputs.thresholds.concentration_max_share
    if holdout.measures is not None:
        m = holdout.measures
        total = m.long_net_pnl + m.short_net_pnl
        _share_check(
            "concentration_direction", max(m.long_net_pnl, m.short_net_pnl), total, limit, "direction_share", out
        )
    nets = sorted((t.net_pnl for t in inputs.holdout_trades), reverse=True)
    if nets:
        top = sum(nets[: math.ceil(len(nets) * inputs.thresholds.top_trades_quantile)], Decimal(0))
        _share_check("concentration_top_trades", top, sum(nets, Decimal(0)), limit, "top_trades_share", out)
    coin_limit = inputs.thresholds.instrument_max_share
    by_coin: dict[str, Decimal] = {}
    for t in inputs.holdout_trades:
        by_coin[t.instrument_id] = by_coin.get(t.instrument_id, Decimal(0)) + t.net_pnl
    if coin_limit is not None and len(by_coin) >= 2:
        total = sum(by_coin.values(), Decimal(0))
        _share_check("concentration_instrument", max(by_coin.values()), total, coin_limit, "instrument_share", out)
