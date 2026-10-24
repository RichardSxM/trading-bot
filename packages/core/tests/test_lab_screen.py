"""covers: spec 0017 AC-9 (the screen rule: each failing reason alone, missing figures, the trade rate note) and
AC-20 (the candle gate floor and the plan flag)."""

from decimal import Decimal

import pytest

from trading_core.lab.screen import (
    CANDLE_GATE_MIN_TRADES_PER_DAY,
    SCREEN_MIN_TRADES_PER_DAY,
    ScreenFigures,
    is_candle_gate,
    screen_verdict,
)

D = Decimal
K = D("1.5")
GOOD = ScreenFigures(
    walk_forward_sharpe=D("0.8"), avg_gross_edge=D(3), avg_round_trip_cost=D(2), trade_count=170, days=10
)


def _with(**changes) -> ScreenFigures:
    return ScreenFigures(**{**vars(GOOD), **changes})


def test_a_screen_passes_when_all_three_hold():
    verdict = screen_verdict(GOOD, K)

    assert (verdict.passed, verdict.reasons) == (True, ())
    assert SCREEN_MIN_TRADES_PER_DAY == 17


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({"walk_forward_sharpe": D(0)}, ("walk_forward_sharpe",)),
        ({"avg_gross_edge": D("2.99")}, ("fee_hurdle",)),
        ({"trade_count": 169}, ("trades_per_day",)),
        ({"walk_forward_sharpe": None}, ("missing:walk_forward_sharpe",)),
        ({"avg_gross_edge": None}, ("missing:avg_gross_edge",)),
        ({"avg_round_trip_cost": None}, ("missing:avg_round_trip_cost",)),
        ({"days": 0}, ("missing:trades_per_day",)),
        ({"trade_count": None, "walk_forward_sharpe": D(-1)}, ("walk_forward_sharpe", "missing:trades_per_day")),
    ],
)
def test_each_failing_reason_fails_the_screen(changes, reasons):
    verdict = screen_verdict(_with(**changes), K)

    assert (verdict.passed, verdict.reasons) == (False, reasons)


def test_a_fail_on_the_trade_rate_alone_is_flagged():
    assert screen_verdict(_with(trade_count=100), K).trade_rate_only
    assert not screen_verdict(_with(trade_count=100, walk_forward_sharpe=D(0)), K).trade_rate_only


def test_a_candle_gate_study_has_the_lower_floor():
    # 2 trades over 10 days is 0.2 a day: under 17, at least 0.17.
    rare = _with(trade_count=2)

    assert D("0.17") == CANDLE_GATE_MIN_TRADES_PER_DAY
    assert screen_verdict(rare, K).reasons == ("trades_per_day",)
    assert screen_verdict(rare, K, CANDLE_GATE_MIN_TRADES_PER_DAY).passed
    assert screen_verdict(_with(trade_count=1), K, CANDLE_GATE_MIN_TRADES_PER_DAY).reasons == ("trades_per_day",)


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ({"study": {"name": "x", "candle_gate": True}}, True),
        ({"study": {"name": "x", "candle_gate": False}}, False),
        ({"study": {"name": "x"}}, False),
        ({"study": {"name": "x", "candle_gate": "true"}}, False),
        ({}, False),
    ],
)
def test_only_a_true_candle_gate_in_the_plan_counts(plan, expected):
    assert is_candle_gate(plan) is expected
