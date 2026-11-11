"""covers: ``trend_vol``'s pure rule: the book volatility of a unit book, the plan rescaled to the annual
target and held under the gross cap, and the no trade band.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_core.strategy.trend_portfolio import DailyClose, PortfolioParams, portfolio_plan, sample_vol
from trading_core.strategy.trend_vol import (
    BookVolParams,
    book_vol_plan,
    unit_book_vol,
    within_band,
)

D = Decimal
DAY0 = date(2026, 1, 1)
PARAMS = PortfolioParams(vol_target=D("0.155"), gross_cap=D(5))


def _series(start, steps):
    closes, price = [], D(start)
    for i, step in enumerate(steps):
        price *= 1 + D(step)
        closes.append(DailyClose(DAY0 + timedelta(days=i), price))
    return closes


def _wave(up, down, n=130):
    return [up if i % 2 == 0 else down for i in range(n)]


def test_one_coin_at_weight_one_has_the_coins_own_vol():
    closes = {"A": _series(100, _wave("0.03", "-0.01"))}

    vol = unit_book_vol(closes, {"A": D(1)}, 30)

    assert vol == sample_vol([c.close for c in closes["A"]], 30)


def test_a_long_and_a_short_in_the_same_coin_path_cancel():
    steps = _wave("0.03", "-0.01")
    closes = {"A": _series(100, steps), "B": _series(50, steps)}

    assert unit_book_vol(closes, {"A": D("0.5"), "B": D("-0.5")}, 30) < D("1e-20")
    # The same two coins held on the same side are one bet: the full vol of either.
    assert unit_book_vol(closes, {"A": D("0.5"), "B": D("0.5")}, 30) == unit_book_vol(closes, {"A": D(1)}, 30)


def test_too_few_closes_for_a_weighted_coin_is_refused():
    closes = {"A": _series(100, _wave("0.03", "-0.01", n=30))}

    with pytest.raises(ValueError, match="31 closes"):
        unit_book_vol(closes, {"A": D(1)}, 30)


def test_the_plan_is_scaled_to_the_target_and_keeps_signals_and_the_split():
    closes = {"A": _series(100, _wave("0.03", "-0.01")), "B": _series(50, _wave("-0.04", "0.02"))}
    base = portfolio_plan(closes, D(1000), PARAMS)
    params = BookVolParams(book_vol_target=D("0.5"), gross_cap=D(5), band=D("0.3"))

    sized = book_vol_plan(closes, base, params)

    plan = sized.plan
    gross = sum(abs(c.weight) for c in plan.coins.values())
    assert sized.unit_vol is not None
    assert sized.unit_vol > 0
    assert gross == pytest.approx(sized.gross)
    assert sized.gross < 5
    # The book's daily vol at the new weights, annualised, is the target.
    weights = {coin: c.weight for coin, c in plan.coins.items()}
    annual = unit_book_vol(closes, weights, 30) * D(365).sqrt()
    assert annual == pytest.approx(D("0.5"))
    for coin, item in plan.coins.items():
        assert item.signal == base.coins[coin].signal
        assert item.target_notional == item.weight * 1000
        assert item.weight / base.coins[coin].weight == pytest.approx(sized.gross / 5)
    assert (plan.n, plan.equity, plan.close_date) == (base.n, base.equity, base.close_date)
    assert plan.gross_scale == pytest.approx(base.gross_scale * sized.gross / 5)


def test_the_gross_cap_binds_for_a_quiet_book():
    closes = {"A": _series(100, _wave("0.0011", "0.0009"))}
    base = portfolio_plan(closes, D(1000), PARAMS)

    sized = book_vol_plan(closes, base, BookVolParams(gross_cap=D(3)))

    assert sized.gross == 3
    assert sized.plan.coins["A"].weight == 3


def test_a_plan_with_no_weight_comes_back_unchanged():
    closes = {"A": _series(100, _wave("0.03", "-0.01", n=40))}  # not eligible: under 120 closes
    base = portfolio_plan(closes, D(1000), PARAMS)

    sized = book_vol_plan(closes, base, BookVolParams())

    assert sized.plan is base
    assert sized.unit_vol is None
    assert sized.gross == 0


@pytest.mark.parametrize(
    ("target", "current", "band", "inside"),
    [
        (1000, 800, "0.3", True),
        (1000, 1300, "0.3", True),
        (1000, 699, "0.3", False),
        (-1000, -750, "0.3", True),
        (-1000, 750, "0.3", False),  # other side: always trade
        (1000, 0, "0.3", False),  # flat: always open
        (0, 500, "0.3", False),  # target 0: always close
        (1000, 999, "0", False),  # band off
    ],
)
def test_the_band(target, current, band, inside):
    assert within_band(D(target), D(current), D(band)) is inside


@pytest.mark.parametrize(
    "bad", [{"book_vol_target": D(0)}, {"vol_days": 1}, {"gross_cap": D(0)}, {"band": D(1)}, {"band": D("-0.1")}]
)
def test_bad_settings_are_refused(bad):
    with pytest.raises(ValueError, match="must be"):
        BookVolParams(**bad)
