"""covers: spec 0008 Result fields (return after costs, max drawdown on realized equity, win rate)."""

from decimal import Decimal

import pytest

from trading_core.lab.metrics import ClosedTrade, FundingCash, max_drawdown, run_metrics

D = Decimal


def test_metrics_of_a_small_run():
    trades = [
        ClosedTrade(closed_ns=1, realized_pnl=D("10"), fees=D("1")),
        ClosedTrade(closed_ns=2, realized_pnl=D("-30"), fees=D("1")),
        ClosedTrade(closed_ns=3, realized_pnl=D("5"), fees=D("1"), funding=D("-6")),
    ]

    metrics = run_metrics(D("1000"), trades, [FundingCash(paid_ns=3, amount=D("-6"))], D("3"))

    assert metrics.net_pnl == D("-21")
    assert metrics.return_after_costs == D("-0.021")
    assert metrics.trade_count == 3
    # Only the first trade is above zero after funding (the third nets -1).
    assert metrics.win_rate == D(1) / D(3)
    assert (metrics.fees_total, metrics.funding_total) == (D("3"), D("-6"))
    # Peak 1010 after the first trade, then 980, then 974 (funding), then 979.
    assert metrics.max_drawdown == (D("1010") - D("974")) / D("1010")


def test_funding_steps_before_a_close_at_the_same_nanosecond():
    # Funding first: 1000 → 1020 (peak) → 1000. Close first would give 980 → 1000, a 2 % drawdown.
    trades = [ClosedTrade(closed_ns=5, realized_pnl=D("-20"), fees=D(0))]
    funding = [FundingCash(paid_ns=5, amount=D("20"))]

    assert max_drawdown(D("1000"), trades, funding) == D("20") / D("1020")


def test_the_starting_balance_is_the_first_peak():
    assert max_drawdown(D("1000"), [ClosedTrade(closed_ns=1, realized_pnl=D("-100"), fees=D(0))], []) == D("0.1")
    assert max_drawdown(D("1000"), [ClosedTrade(closed_ns=1, realized_pnl=D("50"), fees=D(0))], []) == D(0)


def test_nothing_closed_gives_zeros():
    metrics = run_metrics(D("1000"), [], [], D(0))

    assert (metrics.trade_count, metrics.win_rate, metrics.max_drawdown, metrics.net_pnl) == (0, D(0), D(0), D(0))


def test_a_balance_of_zero_is_refused():
    with pytest.raises(ValueError, match="starting balance"):
        run_metrics(D(0), [], [], D(0))
