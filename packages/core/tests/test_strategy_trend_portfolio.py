"""The trend portfolio rule (spec 0030 AC-1, AC-2)."""

from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from trading_core.strategy.trend_portfolio import (
    BadCloseError,
    DailyClose,
    EquityNotPositiveError,
    PortfolioParams,
    order_delta,
    portfolio_plan,
    sample_vol,
)

START = date(2026, 1, 1)
PARAMS = PortfolioParams()


def series(values, start=START):
    return [DailyClose(start + timedelta(days=i), Decimal(v)) for i, v in enumerate(values)]


def trend(n, first, step):
    """``n`` closes from ``first``, adding ``step`` each day, then a wiggle so vol is not 0."""
    return [Decimal(first) + Decimal(step) * i + (Decimal("0.5") if i % 2 else 0) for i in range(n)]


def hand_vol(values, days=30):
    window = values[-(days + 1) :]
    rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    with localcontext() as ctx:
        ctx.prec = 28
        return var.sqrt()


def test_three_coins_give_hand_computed_signal_vol_weight_and_n():
    up = trend(130, 100, 1)
    down = trend(130, 500, -1)
    flat_ish = trend(130, 200, "0.1")
    closes = {"A": series(up), "B": series(down), "C": series(flat_ish)}
    plan = portfolio_plan(closes, Decimal(1000), PARAMS)
    assert plan.n == 3
    assert plan.coins["A"].signal == 1
    assert plan.coins["B"].signal == -1
    for coin, values in (("A", up), ("B", down), ("C", flat_ish)):
        vol = hand_vol(values)
        assert plan.coins[coin].vol == vol
        raw = plan.coins[coin].signal * Decimal("0.02") / vol
        expected = raw / 3 * plan.gross_scale
        assert plan.coins[coin].weight == expected
        assert plan.coins[coin].target_notional == expected * 1000
        assert plan.coins[coin].close_date == START + timedelta(days=129)
    assert plan.close_date == START + timedelta(days=129)


def test_gross_over_cap_scales_every_weight():
    # Very calm coins: raw weights are huge, so the gross is far over 2 and gets scaled to exactly 2.
    calm = [Decimal(100) + (Decimal("0.01") if i % 2 else 0) + Decimal("0.001") * i for i in range(130)]
    plan = portfolio_plan({"A": series(calm), "B": series(calm)}, Decimal(500), PARAMS)
    assert plan.gross > 2
    assert plan.gross_scale == Decimal(2) / plan.gross
    total = sum((abs(c.weight) for c in plan.coins.values()), Decimal(0))
    assert abs(total - 2) < Decimal("1e-20")


def test_gross_of_three_scales_by_two_thirds():
    params = PortfolioParams(vol_target=Decimal(1))
    values = trend(130, 100, 1)
    vol = sample_vol(values, 30)
    # Choose the target so one coin's raw weight is exactly 3: gross 3 → scale 2/3.
    params = PortfolioParams(vol_target=3 * vol)
    plan = portfolio_plan({"A": series(values)}, Decimal(100), params)
    assert plan.gross == 3
    assert plan.gross_scale == Decimal(2) / 3


def test_short_history_is_not_eligible_and_gets_zero():
    plan = portfolio_plan({"A": series(trend(119, 100, 1)), "B": series(trend(120, 100, 1))}, Decimal(1000), PARAMS)
    assert not plan.coins["A"].eligible
    assert plan.coins["A"].weight == 0
    assert plan.coins["A"].target_notional == 0
    assert plan.coins["B"].eligible
    assert plan.n == 1


def test_equal_closes_give_signal_zero():
    values = trend(130, 100, 1)
    values[-31] = values[-1]
    plan = portfolio_plan({"A": series(values)}, Decimal(1000), PARAMS)
    assert plan.coins["A"].signal == 0
    assert plan.coins["A"].weight == 0


def test_zero_vol_gives_weight_zero_but_counts_in_n():
    still = [Decimal(100)] * 130
    plan = portfolio_plan({"A": series(still), "B": series(trend(130, 100, 1))}, Decimal(1000), PARAMS)
    assert plan.coins["A"].eligible
    assert plan.coins["A"].vol == 0
    assert plan.coins["A"].weight == 0
    assert plan.n == 2
    vol_b = hand_vol(trend(130, 100, 1))
    assert plan.coins["B"].weight == Decimal("0.02") / vol_b / 2 * plan.gross_scale


@pytest.mark.parametrize("equity", [Decimal(0), Decimal(-1)])
def test_equity_at_or_below_zero_raises(equity):
    with pytest.raises(EquityNotPositiveError):
        portfolio_plan({"A": series(trend(130, 100, 1))}, equity, PARAMS)


def test_days_out_of_order_raise():
    bad = series(trend(130, 100, 1))
    bad[5], bad[6] = bad[6], bad[5]
    with pytest.raises(BadCloseError):
        portfolio_plan({"A": bad}, Decimal(1000), PARAMS)


def test_no_coins_gives_an_empty_plan():
    plan = portfolio_plan({}, Decimal(1000), PARAMS)
    assert plan.n == 0
    assert plan.gross == 0
    assert plan.gross_scale == 1
    assert plan.close_date is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback_days": 1},
        {"vol_days": 1},
        {"vol_target": Decimal(0)},
        {"gross_cap": Decimal(0)},
        {"min_history_days": 30},
    ],
)
def test_bad_params_refuse(kwargs):
    with pytest.raises(ValueError, match="must be"):
        PortfolioParams(**kwargs)


def test_delta_rounds_target_toward_zero_to_the_step():
    # 0.0123 BTC at 100 000: target notional 1230.
    delta = order_delta(Decimal(1230), Decimal(100_000), Decimal(0), Decimal("0.0001"), None)
    assert delta == Decimal("0.0123")
    assert order_delta(Decimal("1239.99"), Decimal(100_000), Decimal(0), Decimal("0.0001"), None) == Decimal("0.0123")
    assert order_delta(Decimal("-1239.99"), Decimal(100_000), Decimal(0), Decimal("0.0001"), None) == Decimal("-0.0123")


def test_delta_is_target_minus_current_signed():
    delta = order_delta(Decimal(1000), Decimal(100_000), Decimal("0.02"), Decimal("0.0001"), None)
    assert delta == Decimal("-0.0100")
    assert order_delta(Decimal(-1000), Decimal(100_000), Decimal("0.01"), Decimal("0.0001"), None) == Decimal("-0.02")


def test_delta_under_the_step_or_minimum_is_skipped():
    assert order_delta(Decimal(1230), Decimal(100_000), Decimal("0.0123"), Decimal("0.0001"), None) is None
    assert order_delta(Decimal(1240), Decimal(100_000), Decimal("0.0123"), Decimal("0.0001"), Decimal("0.001")) is None
    assert order_delta(Decimal(1340), Decimal(100_000), Decimal("0.0123"), Decimal("0.0001"), Decimal("0.001")) == (
        Decimal("0.0011")
    )


def test_delta_refuses_a_bad_mid():
    with pytest.raises(ValueError, match="above 0"):
        order_delta(Decimal(1), Decimal(0), Decimal(0), Decimal(1), None)
