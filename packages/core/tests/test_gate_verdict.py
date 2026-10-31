"""covers: spec 0015 AC-4 (the shared benchmark rule), AC-10, AC-12 (the checks, the inconsistent evidence
reasons, and the fail before insufficient before pass rule); spec 0022 AC-13 (``concentration_instrument``,
and a single coin study judged the same under thresholds v1 and v2); spec 0027 AC-5 to AC-8 (the ``trend`` kind's
direction, top trades, and benchmark rules, and ``prior_trials`` in the metrics)."""

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
BTC, ETH = "PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN"


def _daily(drift: float, days: int = 120, seed: int = 1, *, development: bool = False) -> tuple[DayEquity, ...]:
    """From the holdout start, or for development the ``days`` that end the day before it."""
    rng = random.Random(seed)
    equity = Decimal(10_000)
    first = START - timedelta(days=days) if development else START
    rows = []
    for i in range(days):
        equity *= Decimal(str(round(1 + drift + rng.gauss(0, 0.01), 6)))
        rows.append(DayEquity(first + timedelta(days=i), equity, 1, Decimal(1), Decimal(0)))
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
        version_frozen_hash=f"h{n}",
    )


def _inputs(*, holdout_drift=0.004, bench_drift=0.0, kind="hour_level", trades=100, **changes) -> CandidateInputs:
    runs = [
        _run(Role.DEVELOPMENT, "macd_x", _daily(0.004, days=300, seed=9, development=True)),
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
        final_grid_point=0,
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
        _run(Role.DEVELOPMENT, "macd_x", _daily(0.0, days=240, seed=s, development=True), grid_point=s, n=300 + s)
        for s in range(20)
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
    trades = tuple(TradeReturn(Decimal(900 if i == 0 else 1), Decimal("0.001"), BTC) for i in range(100))

    assert ("concentration_top_trades", "failed") in _codes(
        evaluate_candidate(replace(_inputs(), holdout_trades=trades))
    )


def test_a_missing_drawdown_or_cost_measure_is_insufficient():
    inputs = _holdout_with(_inputs(), measures=None)

    codes = _codes(evaluate_candidate(inputs))

    assert ("drawdown_budget", "missing_metric") in codes
    assert ("fee_hurdle", "missing_cost") in codes
    assert evaluate_candidate(inputs).verdict == GateVerdict.INSUFFICIENT


def test_two_completed_holdouts_score_neither():
    inputs = _inputs()
    holdout = inputs.holdout()
    assert holdout is not None
    second = replace(holdout, run_id=UUID(int=99), grid_point=1, frozen_hash="h9", version_frozen_hash="h9")
    both = replace(inputs, runs=(*inputs.runs, second))

    result = evaluate_candidate(both)

    assert both.holdout() is None
    assert _codes(result) == [("holdout", "inconsistent_evidence")]
    assert result.verdict == GateVerdict.INSUFFICIENT
    assert "holdout_run_id" not in result.metrics


def test_a_study_run_hash_that_is_not_its_versions_is_insufficient():
    inputs = _holdout_with(_inputs(), version_frozen_hash="other")

    result = evaluate_candidate(inputs)

    assert _codes(result) == [("frozen_version", "inconsistent_evidence")]
    assert result.verdict == GateVerdict.INSUFFICIENT


def test_a_holdout_of_another_grid_point_than_the_winner_is_insufficient():
    for final in (1, None):
        result = evaluate_candidate(_inputs(final_grid_point=final))

        assert _codes(result) == [("holdout_grid_point", "inconsistent_evidence")]
        assert result.verdict == GateVerdict.INSUFFICIENT


def test_days_on_the_wrong_side_of_the_holdout_start_are_insufficient():
    inputs = _inputs()
    late = replace(
        inputs,
        runs=tuple(
            replace(r, daily=_daily(0.004, days=300, seed=9)) if r.role == Role.DEVELOPMENT else r for r in inputs.runs
        ),
    )
    early = _holdout_with(inputs, daily=_daily(0.004, days=150, seed=1, development=True) + _daily(0.004))

    assert ("development_days", "inconsistent_evidence") in _codes(evaluate_candidate(late))
    assert ("holdout_days", "inconsistent_evidence") in _codes(evaluate_candidate(early))
    for result in map(evaluate_candidate, (late, early)):
        assert result.verdict != GateVerdict.PASS


def test_folds_and_bootstrap_bounds_are_stored_and_repeat_exactly():
    first, second = evaluate_candidate(_inputs()), evaluate_candidate(_inputs())

    assert first.folds == second.folds
    assert first.metrics["bootstrap_lower_bound"] == second.metrics["bootstrap_lower_bound"]
    assert Decimal(str(first.metrics["bootstrap_lower_bound"])) > 0


def _coin_trades(btc: int, eth: int) -> tuple[TradeReturn, ...]:
    """50 trades on each coin summing to ``btc`` and ``eth`` USD, small enough to pass the top trades share."""
    return tuple(
        TradeReturn(Decimal(total) / 50, Decimal("0.001"), coin)
        for coin, total in ((BTC, btc), (ETH, eth))
        for _ in range(50)
    )


def test_one_coin_with_most_of_the_profit_fails_the_coin_share():
    result = evaluate_candidate(replace(_inputs(), holdout_trades=_coin_trades(700, 300)))

    assert ("concentration_instrument", "failed") in _codes(result)
    assert result.metrics["instrument_share"] == "0.700000"


def test_a_coin_share_under_the_bar_passes():
    result = evaluate_candidate(replace(_inputs(), holdout_trades=_coin_trades(550, 450)))

    assert "concentration_instrument" not in {check for check, _ in _codes(result)}
    assert result.metrics["instrument_share"] == "0.550000"


def test_one_coin_or_no_profit_skips_the_coin_share():
    one_coin = tuple(replace(t, instrument_id=BTC) for t in _coin_trades(700, 300))
    for trades in (one_coin, _coin_trades(-700, 300)):
        result = evaluate_candidate(replace(_inputs(), holdout_trades=trades))

        assert "concentration_instrument" not in {check for check, _ in _codes(result)}
    assert "instrument_share" not in evaluate_candidate(replace(_inputs(), holdout_trades=one_coin)).metrics


def test_a_single_coin_study_gets_the_same_verdict_under_v1_and_v2():
    v1 = replace(THRESHOLDS, version=1, instrument_max_share=None, trend=None)
    trades = tuple(replace(t, instrument_id=BTC) for t in _coin_trades(700, 300))
    for inputs in (_inputs(), _inputs(holdout_drift=-0.004), replace(_inputs(), holdout_trades=trades)):
        under_v2 = evaluate_candidate(inputs)
        under_v1 = evaluate_candidate(replace(inputs, thresholds=v1))

        assert (under_v1.verdict, under_v1.reasons, under_v1.metrics) == (
            under_v2.verdict,
            under_v2.reasons,
            under_v2.metrics,
        )


# Spec 0027: the trend kind ---------------------------------------------------------------------

LONG_ONLY = replace(MEASURES, long_trades=100, long_net_pnl=Decimal(950), short_trades=0, short_net_pnl=Decimal(0))


def _trend(**changes) -> CandidateInputs:
    """A long only trend study that passes: buy and hold is its only benchmark, so no MACD run."""
    inputs = _holdout_with(_inputs(kind="trend", long_only=True, **changes), measures=LONG_ONLY)
    return replace(inputs, runs=tuple(r for r in inputs.runs if r.strategy != MACD_BASELINE))


def _tail_trades() -> tuple[TradeReturn, ...]:
    """100 trades whose top 5 carry 1.4 times the net profit (five of 280 against a total of 1000)."""
    return tuple(
        TradeReturn(Decimal(280 if i < 5 else -400) / (1 if i < 5 else 95), Decimal("0.001"), BTC) for i in range(100)
    )


def test_a_long_only_trend_study_skips_the_direction_check_and_records_its_share():
    result = evaluate_candidate(_trend())

    assert result.reasons == ()
    assert result.verdict == GateVerdict.PASS
    assert result.metrics["direction_applicable"] is False
    assert result.metrics["direction_share"] == "1.000000"
    assert result.metrics["top_trades_applicable"] is False


def test_a_short_trade_in_a_long_only_holdout_fails():
    inputs = _holdout_with(_trend(), measures=replace(LONG_ONLY, short_trades=1, short_net_pnl=Decimal(-5)))

    result = evaluate_candidate(inputs)

    assert _codes(result) == [("long_only_violated", "failed")]
    assert result.reasons[0].value == "1"
    assert result.verdict == GateVerdict.FAIL


def test_a_long_only_holdout_without_measures_fails_closed():
    codes = _codes(evaluate_candidate(_holdout_with(_trend(), measures=None)))

    assert ("long_only_violated", "missing_metric") in codes
    assert "concentration_direction" not in {check for check, _ in codes}


def test_a_trend_study_not_declared_long_only_keeps_the_direction_check():
    one_sided = replace(LONG_ONLY, long_net_pnl=Decimal(900), short_trades=10, short_net_pnl=Decimal(100))
    result = evaluate_candidate(_holdout_with(replace(_trend(), long_only=False), measures=one_sided))

    assert _codes(result) == [("concentration_direction", "failed")]
    assert result.metrics["direction_applicable"] is True


def test_long_only_changes_nothing_for_an_hour_level_study():
    one_sided = replace(MEASURES, long_net_pnl=Decimal(900), short_net_pnl=Decimal(100))
    for long_only in (False, True):
        result = evaluate_candidate(_holdout_with(_inputs(long_only=long_only), measures=one_sided))

        assert _codes(result) == [("concentration_direction", "failed")]
        assert result.metrics["direction_applicable"] is True
        assert result.metrics["top_trades_applicable"] is True


def test_a_trend_study_records_the_top_trades_share_without_failing_it():
    trend = evaluate_candidate(replace(_trend(), holdout_trades=_tail_trades()))
    scalping = _inputs(kind="scalping", holdout_trades=_tail_trades())
    scalping = replace(scalping, runs=tuple(r for r in scalping.runs if r.strategy != MACD_BASELINE))

    assert "concentration_top_trades" not in {check for check, _ in _codes(trend)}
    assert trend.metrics["top_trades_share"] == "1.400000"
    assert _codes(evaluate_candidate(scalping)) == [("concentration_top_trades", "failed")]


def test_a_trend_study_is_judged_against_buy_and_hold_only():
    losing = evaluate_candidate(_trend(holdout_drift=-0.004, bench_drift=0.0))

    assert benchmark_names("trend", "tsmom") == (BUY_AND_HOLD,)
    assert ("beats_buy_and_hold", "failed") in _codes(losing)
    assert "beats_macd_baseline_applicable" not in evaluate_candidate(_trend()).metrics


def test_prior_trials_are_recorded_and_zero_for_other_kinds():
    assert evaluate_candidate(_trend(prior_trials=13, trial_count=17)).metrics["prior_trials"] == 13
    assert evaluate_candidate(_trend(prior_trials=13, trial_count=17)).metrics["trial_count"] == 17
    assert evaluate_candidate(_inputs()).metrics["prior_trials"] == 0
