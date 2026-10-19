"""covers: spec 0015 AC-16 (the paper → live checks over a chained realized paper record)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_core.gate.paper import (
    EntryFill,
    EntryOrder,
    PaperEvent,
    PaperInputs,
    entry_slippage_bps,
    evaluate_paper,
    paper_record,
)
from trading_core.gate.thresholds import parse_thresholds
from trading_core.gate.verdict import GateVerdict
from trading_core.versioning import package_root

THRESHOLDS = parse_thresholds((package_root() / "gate" / "thresholds.toml").read_bytes())
START = datetime(2026, 9, 1, tzinfo=UTC)


def _inputs(**changes) -> PaperInputs:
    events = tuple(
        PaperEvent(START + timedelta(hours=6 * i), Decimal(3) if i % 3 else Decimal(-1), True) for i in range(120)
    )
    base = PaperInputs(
        frozen_hash="f" * 64,
        strategy_kind="scalping",
        thresholds=THRESHOLDS,
        evaluated_at=START + timedelta(days=31),
        gate_pass=True,
        entry_mode="taker",
        first_started_at=START,
        starting_balance=Decimal(10_000),
        events=events,
        entries=(),
        entry_fills=tuple(EntryFill(True, Decimal("60003"), Decimal(60000)) for _ in range(10)),
        holdout_trade_bound=Decimal("0.00001"),
        lab_fill_rate=Decimal("0.6"),
        lab_slippage_bps=Decimal("0.5"),
    )
    return replace(base, **changes)


def _codes(result):
    return sorted((r.check, r.code) for r in result.reasons)


def test_a_good_paper_record_passes():
    result = evaluate_paper(_inputs())

    assert result.reasons == ()
    assert result.verdict == GateVerdict.PASS
    assert result.metrics["paper_trades"] == 120


def test_twenty_nine_days_is_insufficient():
    result = evaluate_paper(_inputs(evaluated_at=START + timedelta(days=29)))

    assert _codes(result) == [("paper_duration", "too_few_days")]
    assert result.verdict == GateVerdict.INSUFFICIENT


def test_a_paper_mean_below_the_lab_bound_fails():
    result = evaluate_paper(_inputs(holdout_trade_bound=Decimal("0.01")))

    assert _codes(result) == [("paper_lab_band", "failed")]


def test_a_maker_fill_rate_far_from_the_lab_fails():
    entries = tuple(EntryOrder(True, i < 4) for i in range(10))

    result = evaluate_paper(_inputs(entry_mode="maker", entries=entries))

    assert _codes(result) == [("fill_rate_drift", "failed")]
    assert Decimal(str(result.metrics["fill_rate_drift"])) == Decimal("0.333333")


def test_slippage_worse_than_the_lab_by_over_two_bps_fails():
    fills = tuple(EntryFill(False, Decimal("59980"), Decimal(60000)) for _ in range(5))

    assert _codes(evaluate_paper(_inputs(entry_fills=fills))) == [("slippage_drift", "failed")]


def test_no_gate_pass_or_no_paper_run_is_insufficient():
    manual = evaluate_paper(_inputs(gate_pass=False))
    empty = evaluate_paper(_inputs(first_started_at=None, starting_balance=None))

    assert ("candidate_pass", "missing_run") in _codes(manual)
    assert manual.verdict == GateVerdict.INSUFFICIENT
    assert ("paper_runs", "missing_run") in _codes(empty)


def test_a_losing_record_fails_net_and_drawdown():
    events = tuple(PaperEvent(START + timedelta(hours=i), Decimal(-15), True) for i in range(120))

    codes = _codes(evaluate_paper(_inputs(events=events)))

    assert ("paper_net_positive", "failed") in codes
    assert ("paper_drawdown_budget", "failed") in codes


def test_the_record_chains_trades_and_funding_from_the_starting_balance():
    events = [
        PaperEvent(START, Decimal(100), True),
        PaperEvent(START + timedelta(hours=1), Decimal(-20), False),
        PaperEvent(START + timedelta(hours=2), Decimal(-80), True),
    ]

    record = paper_record(Decimal(1000), events)

    assert record.trades == 2
    assert record.paper_return == Decimal(0)
    assert record.trade_returns == (Decimal("0.1"), Decimal(-80) / Decimal(1080))
    assert record.max_drawdown == Decimal(100) / Decimal(1100)


def test_slippage_is_signed_so_worse_for_the_trade_is_positive():
    assert entry_slippage_bps(EntryFill(True, Decimal(60006), Decimal(60000))) == Decimal(1)
    assert entry_slippage_bps(EntryFill(False, Decimal(60006), Decimal(60000))) == Decimal(-1)
    assert entry_slippage_bps(EntryFill(True, Decimal(1), None)) is None
