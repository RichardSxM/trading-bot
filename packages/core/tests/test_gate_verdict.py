"""covers: spec 0015 AC-4 (the shared benchmark rule), AC-10, AC-12 (the checks and the fail before
insufficient before pass rule)."""

import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from trading_core.gate.checks import Reason, benchmark_names
from trading_core.gate.inputs import (
    BUY_AND_HOLD,
    MACD_BASELINE,
    CandidateInputs,
    Lab,
    Measures,
    Role,
    RunSummary,
    StudyRunInput,
    TradeReturn,
)
from trading_core.gate.series import DayEquity
from trading_core.gate.thresholds import parse_thresholds
from trading_core.gate.verdict import GateVerdict, decide, evaluate_candidate
from trading_core.versioning import package_root

THRESHOLDS = parse_thresholds((package_root() / "gate" / "thresholds.toml").read_bytes())
START = date(2025, 10, 19)


def _daily(drift: float, days: int = 120, seed: int = 1) -> tuple[DayEquity, ...]:
    rng = random.Random(seed)
    equity = Decimal(10_000)
    rows = []
    for i in range(days):
        equity *= Decimal(str(round(1 + drift + rng.gauss(0, 0.01), 6)))
        rows.append(DayEquity(START + timedelta(days=i), equity, 1, Decimal(1), Decimal(0)))
    return tuple(rows)


def _summary(trades: int = 100, fees: Decimal = Decimal(50)) -> RunSummary:
    return RunSummary(Decimal("0.2"), Decimal("0.05"), Decimal("0.5"), trades, fees, Decimal(0))


MEASURES = Measures(
    drawdown_basis="marked",
    marked_max_drawdown=Decimal("0.05"),
    long_trades=50,
    long_net_pnl=Decimal(500),
    short_trades=50,
    short_net_pnl=Decimal(450),
    top_trades_profit_share=Decimal("0.1"),
    avg_gross_edge=Decimal(10),
    avg_round_trip_cost=Decimal(2),
)


def _run(role: Role, strategy: str, daily, *, grid_point: int | None = 0, summary=None, n: int = 0) -> StudyRunInput:
    return StudyRunInput(
        run_id=UUID(int=n + (1 if role == Role.HOLDOUT else 100 if role == Role.BENCHMARK else 200)),
        role=role,
        strategy=strategy,
        grid_point=None if role == Role.BENCHMARK else grid_point,
        frozen_hash=f"h{n}",
        status="completed",
        starting_balance=Decimal(10_000),
        daily=daily,
        summary=summary or _summary(),
        measures=MEASURES,
    )


def _inputs(*, holdout_drift=0.004, bench_drift=0.0, kind="hour_level", trades=100, **changes) -> CandidateInputs:
    runs = [
        _run(Role.DEVELOPMENT, "macd_x", _daily(0.004, days=300, seed=9)),
        _run(Role.HOLDOUT, "macd_x", _daily(holdout_drift), summary=_summary(trades)),
        _run(Role.BENCHMARK, BUY_AND_HOLD, _daily(bench_drift, seed=2), n=1),
        _run(Role.BENCHMARK, MACD_BASELINE, _daily(bench_drift, seed=3), n=2),
    ]
    base = CandidateInputs(
        study_id=UUID(int=7),
        lab=Lab.CANDLE,
        strategy_name="macd_x",
        strategy_kind=kind,
        thresholds=THRESHOLDS,
        seed=1,
        code_hash="c" * 64,
        runs=tuple(runs),
        fold_overrides=(("first_in_sample_months", 1), ("step_months", 1)),
    )
    return replace(base, **changes)


def _codes(result):
    return sorted((r.check, r.code) for r in result.reasons)


def test_a_strong_study_passes_every_check():
    result = evaluate_candidate(_inputs())

    assert result.reasons == ()
    assert len(result.folds) == 8  # 300 days: 1 month in sample, then 8 whole 1 month steps
    assert result.metrics["pbo_applicable"] is False
    assert result.verdict == GateVerdict.PASS
    assert Decimal(str(result.metrics["holdout_sharpe"])) > Decimal(str(result.metrics["buy_and_hold_sharpe"]))


def test_too_few_trades_is_insufficient_not_a_pass():
    result = evaluate_candidate(_inputs(trades=59))

    assert _codes(result) == [("trade_floor", "too_few_trades")]
    assert result.verdict == GateVerdict.INSUFFICIENT


def test_a_losing_holdout_fails_on_sharpe_and_both_benchmarks():
    result = evaluate_candidate(_inputs(holdout_drift=-0.004, bench_drift=0.0))

    assert ("holdout_sharpe", "failed") in _codes(result)
    assert ("beats_buy_and_hold", "failed") in _codes(result)
    assert ("bootstrap_lower_bound", "failed") in _codes(result)
    assert result.verdict == GateVerdict.FAIL


def test_a_scalping_study_needs_only_buy_and_hold():
    inputs = _inputs(kind="scalping")
    inputs = replace(inputs, runs=tuple(r for r in inputs.runs if r.strategy != MACD_BASELINE))

    assert evaluate_candidate(inputs).verdict == GateVerdict.PASS


def test_the_benchmark_rule_drops_the_studied_strategy_by_name():
    assert benchmark_names("hour_level", "macd_x") == (BUY_AND_HOLD, MACD_BASELINE)
    assert benchmark_names("hour_level", MACD_BASELINE) == (BUY_AND_HOLD,)
    assert benchmark_names("scalping", "random_taker") == (BUY_AND_HOLD,)
    assert benchmark_names("hour_level", BUY_AND_HOLD) == (MACD_BASELINE,)


def test_a_study_of_a_benchmark_strategy_skips_that_comparison():
    inputs = _inputs(strategy_name=MACD_BASELINE)
    inputs = replace(
        inputs, runs=tuple(r for r in inputs.runs if r.strategy != MACD_BASELINE or r.role != Role.BENCHMARK)
    )

    result = evaluate_candidate(inputs)

    assert result.reasons == ()
    assert result.verdict == GateVerdict.PASS
    assert result.metrics["beats_macd_baseline_applicable"] is False
    assert result.metrics["macd_baseline_sharpe"] is None
    assert result.metrics["beats_buy_and_hold_applicable"] is True


def test_missing_evidence_is_insufficient_and_never_a_pass():
    inputs = _inputs()
    no_bench = replace(inputs, runs=tuple(r for r in inputs.runs if r.strategy != BUY_AND_HOLD))
    no_holdout = replace(inputs, runs=tuple(r for r in inputs.runs if r.role != Role.HOLDOUT))
    no_hash = replace(inputs, code_hash=None)
    holdout = inputs.holdout()
    assert holdout is not None
    free = replace(holdout, summary=_summary(fees=Decimal(0)))
    no_fees = replace(inputs, runs=tuple(free if r is holdout else r for r in inputs.runs))

    assert _codes(evaluate_candidate(no_bench)) == [("beats_buy_and_hold", "missing_benchmark")]
    assert _codes(evaluate_candidate(no_holdout)) == [("holdout", "missing_run")]
    assert _codes(evaluate_candidate(no_hash)) == [("frozen_version", "null_code_hash")]
    assert _codes(evaluate_candidate(no_fees)) == [("costs", "missing_cost")]
    for result in map(evaluate_candidate, (no_bench, no_holdout, no_hash, no_fees)):
        assert result.verdict == GateVerdict.INSUFFICIENT


def test_a_failed_holdout_does_not_count():
    inputs = _inputs()
    failed = [replace(r, status="failed", summary=None) if r.role == Role.HOLDOUT else r for r in inputs.runs]

    assert _codes(evaluate_candidate(replace(inputs, runs=tuple(failed)))) == [("holdout", "missing_run")]


def test_a_holdout_without_a_starting_balance_does_not_count():
    inputs = _inputs()
    no_balance = [replace(r, starting_balance=None) if r.role == Role.HOLDOUT else r for r in inputs.runs]

    result = evaluate_candidate(replace(inputs, runs=tuple(no_balance)))
    assert _codes(result) == [("holdout", "missing_run")]
    assert result.verdict == GateVerdict.INSUFFICIENT


def test_fail_beats_insufficient_beats_pass():
    fail, short = Reason("failed", "holdout_sharpe"), Reason("too_few_trades", "trade_floor")

    assert decide([short, fail]) == GateVerdict.FAIL
    assert decide([short]) == GateVerdict.INSUFFICIENT
    assert decide([]) == GateVerdict.PASS


def test_the_same_inputs_give_the_same_metrics():
    assert evaluate_candidate(_inputs()).metrics == evaluate_candidate(_inputs()).metrics


def _holdout_with(inputs: CandidateInputs, **changes) -> CandidateInputs:
    holdout = inputs.holdout()
    assert holdout is not None
    return replace(inputs, runs=tuple(replace(r, **changes) if r is holdout else r for r in inputs.runs))


def test_a_known_loser_fails_on_the_fee_hurdle_and_the_holdout_sharpe():
    """The random_taker shape: costs bigger than the edge, and a negative holdout."""
    loser = _holdout_with(
        _inputs(holdout_drift=-0.003, kind="scalping"),
        measures=replace(MEASURES, avg_gross_edge=Decimal("0.5"), avg_round_trip_cost=Decimal(1)),
    )

    codes = _codes(evaluate_candidate(loser))

    assert ("fee_hurdle", "failed") in codes
    assert ("holdout_sharpe", "failed") in codes


def test_twenty_noise_grid_points_fail_pbo_and_the_deflated_sharpe():
    base = _inputs(holdout_drift=0.004)
    noise = [
        _run(Role.DEVELOPMENT, "macd_x", _daily(0.0, days=240, seed=s), grid_point=s, n=300 + s) for s in range(20)
    ]
    base_holdout = base.holdout()
    assert base_holdout is not None
    holdout = replace(base_holdout, grid_point=0)
    runs = (*noise, holdout, *base.by_role(Role.BENCHMARK))

    result = evaluate_candidate(replace(base, runs=runs, trial_count=20))

    assert Decimal(str(result.metrics["pbo"])) > Decimal("0.30")
    assert ("pbo", "failed") in _codes(result)
    assert ("deflated_sharpe", "failed") in _codes(result)


def test_a_drawdown_over_budget_and_a_one_sided_profit_fail():
    inputs = _holdout_with(
        _inputs(),
        measures=replace(MEASURES, marked_max_drawdown=Decimal("0.2"), short_net_pnl=Decimal(-100)),
    )

    codes = _codes(evaluate_candidate(inputs))

    assert ("drawdown_budget", "failed") in codes
    assert ("concentration_direction", "failed") in codes


def test_a_few_outsized_trades_fail_the_top_trade_share():
    trades = tuple(TradeReturn(Decimal(900 if i == 0 else 1), Decimal("0.001")) for i in range(100))

    assert ("concentration_top_trades", "failed") in _codes(
        evaluate_candidate(replace(_inputs(), holdout_trades=trades))
    )


def test_a_missing_drawdown_or_cost_measure_is_insufficient():
    inputs = _holdout_with(_inputs(), measures=None)

    codes = _codes(evaluate_candidate(inputs))

    assert ("drawdown_budget", "missing_metric") in codes
    assert ("fee_hurdle", "missing_cost") in codes
    assert evaluate_candidate(inputs).verdict == GateVerdict.INSUFFICIENT


def test_folds_and_bootstrap_bounds_are_stored_and_repeat_exactly():
    first, second = evaluate_candidate(_inputs()), evaluate_candidate(_inputs())

    assert first.folds == second.folds
    assert first.metrics["bootstrap_lower_bound"] == second.metrics["bootstrap_lower_bound"]
    assert Decimal(str(first.metrics["bootstrap_lower_bound"])) > 0
