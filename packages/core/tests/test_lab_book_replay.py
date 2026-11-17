"""covers: spec 0033 AC-9, the pure book replay: plan at the cut hour, fill at the H:05 close (no same bar
fill), the trader's per coin step (close missing, band, too small), cost on traded notional, hourly funding with
missing rates counted, marked equity, closed positions, and the period figures.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_core.lab.book_replay import (
    FILL_DELAY_NS,
    CoinData,
    CoinSpec,
    DayResult,
    Planned,
    ReplayConfig,
    correlation,
    cut_ns,
    first_ready_day,
    measures,
    replay_book,
    trend_vol_planner,
)
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import CoinPlan, PortfolioParams, PortfolioPlan
from trading_core.strategy.trend_vol import BookVolParams

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
D0 = date(2024, 1, 1)
STEP = CoinSpec(Decimal("0.001"))


def series(first: date, closes, fills=None, hour=0, funding=None, hourly_extra=None):
    """A coin with one close a day at ``hour`` from ``first`` and an H:05 minute close (``fills`` or the close)."""
    hourly, minutes = {}, {}
    for i, close in enumerate(closes):
        day = first + timedelta(days=i)
        ns = cut_ns(day, hour)
        hourly[ns] = Decimal(close)
        fill = close if fills is None else fills[i]
        if fill is not None:
            minutes[ns + FILL_DELAY_NS] = Decimal(fill)
    hourly.update(hourly_extra or {})
    return CoinData(hourly, minutes, funding or {}, STEP)


def fixed(targets, band=Decimal(0), eligible=True):
    """A planner that always targets ``targets`` (signed USD), each coin's close date the newest it was given."""

    def plan(closes, equity):
        coins = {
            coin: CoinPlan(
                eligible, 1, Decimal("0.01"), Decimal(0), Decimal(targets.get(coin, 0)), (s[-1].day if s else None)
            )
            for coin, s in closes.items()
        }
        return Planned(PortfolioPlan(coins, len(coins), Decimal(1), Decimal(1), equity, None), band, None, Decimal(1))

    return plan


def config(start=D0, days=3, **kw):
    return ReplayConfig(cut_hour=kw.pop("cut_hour", 0), start=start, end=start + timedelta(days=days - 1), **kw)


def test_fill_is_the_h05_close_not_the_signal_bar():
    # The 00:00 close says 100, the 00:05 minute says 110: the trade fills at 110 and pays 7 bps on 1100.
    coins = {BTC: series(D0, ["100", "100", "100"], fills=["110", "110", "110"])}
    result = replay_book(coins, fixed({BTC: "1100"}), config(cost=Decimal("0.0007")))
    first = result.days[0]
    assert first.traded == 1
    assert first.fees == Decimal("1100") * Decimal("0.0007")
    assert first.equity == Decimal(10000) - first.fees
    assert first.fill_ns == cut_ns(D0, 0) + FILL_DELAY_NS


def test_cut_hour_moves_the_plan_and_the_fill():
    coins = {BTC: series(D0, ["100", "100", "100"], fills=["100", "100", "100"], hour=6)}
    result = replay_book(coins, fixed({BTC: "1000"}), config(cut_hour=6))
    assert result.days[0].fill_ns == cut_ns(D0, 6) + FILL_DELAY_NS
    assert result.days[0].traded == 1


def test_marked_equity_follows_the_fill_prices():
    coins = {BTC: series(D0, ["100", "100", "120"], fills=["100", "100", "120"])}
    result = replay_book(coins, fixed({BTC: "1000"}), config(cost=Decimal(0)))
    # 10 coins bought at 100; at 120 they are worth 200 more. The day 3 plan then trims back to 1000 USD.
    assert result.days[2].equity == Decimal(10200)
    assert result.days[2].traded == 1
    assert result.days[2].gross == Decimal("8.333") * 120 / Decimal(10200)


def test_close_missing_keeps_the_position():
    # Day 2 has no 00:00 close, so its plan's newest close is day 0's: the coin keeps its position.
    coins = {BTC: series(D0, ["100", "100", "100"])}
    del coins[BTC].hourly[cut_ns(D0 + timedelta(days=1), 0)]
    result = replay_book(coins, fixed({BTC: "1000"}), config())
    assert [d.close_missing for d in result.days] == [0, 1, 0]


def test_no_fill_is_counted_and_not_traded():
    coins = {BTC: series(D0, ["100", "100", "100"], fills=[None, "100", "100"])}
    result = replay_book(coins, fixed({BTC: "1000"}), config())
    assert (result.days[0].no_fill, result.days[0].traded) == (1, 0)
    assert result.days[1].traded == 1


def test_band_and_small_skips():
    coins = {BTC: series(D0, ["100", "100", "110"])}
    result = replay_book(coins, fixed({BTC: "1000"}, band=Decimal("0.3")), config(cost=Decimal(0)))
    assert result.days[0].traded == 1
    assert result.days[1].band == 1  # exactly on target is within the band
    assert result.days[2].band == 1  # 1100 against 1000 is 10 % away, inside 30 %
    no_band = replay_book(coins, fixed({BTC: "1000"}), config(cost=Decimal(0)))
    assert no_band.days[1].small == 1  # band 0: on target, the delta is 0


def test_flip_closes_one_position_and_opens_another():
    calls = iter(["1000", "-1000", "0"])

    def flipping(closes, equity):
        return fixed({BTC: next(calls)})(closes, equity)

    coins = {BTC: series(D0, ["100", "100", "100"])}
    result = replay_book(coins, flipping, config(cost=Decimal(0)))
    assert [d.closed for d in result.days] == [0, 1, 1]
    assert result.full.closed == 2


def test_planner_error_keeps_the_old_plan_and_does_not_trade():
    calls = iter([None, "boom", None])

    def flaky(closes, equity):
        if next(calls) == "boom":
            raise ValueError("no plan")
        return fixed({BTC: "1000"})(closes, equity)

    coins = {BTC: series(D0, ["100", "100", "100"])}
    result = replay_book(coins, flaky, config())
    assert [d.planned for d in result.days] == [True, False, True]
    assert result.days[1].traded == 0


def test_funding_hourly_and_missing_rates_counted():
    # Long 10 BTC at 100 from day 1 00:05; rates at the 01:00 and 02:00 charges only. A long pays a positive rate.
    first_charge = cut_ns(D0, 0) + HOUR_NS
    rates = {first_charge: Decimal("0.0001"), first_charge + HOUR_NS: Decimal("0.0001")}
    extra = {first_charge + i * HOUR_NS: Decimal(100) for i in range(23)}
    coins = {BTC: series(D0, ["100", "100"], funding=rates, hourly_extra=extra)}
    result = replay_book(coins, fixed({BTC: "1000"}), config(days=2, cost=Decimal(0)))
    assert result.days[1].funding == Decimal("-0.2")  # 2 x -(10 x 100 x 0.0001)
    assert result.funding_missing_days == 2  # the 03:00 to 23:00 charges of day 1 and the 00:00 one of day 2


def test_ineligible_coin_is_flattened():
    def plan(closes, equity):
        base = fixed({BTC: "1000", ETH: "1000"})(closes, equity)
        coins = dict(base.plan.coins)
        if len(closes[ETH]) >= 2:
            coins[ETH] = CoinPlan(False, 0, None, Decimal(0), Decimal(0), closes[ETH][-1].day)
        return Planned(PortfolioPlan(coins, 1, Decimal(1), Decimal(1), equity, None), Decimal(0), None, Decimal(1))

    coins = {BTC: series(D0, ["100"] * 3), ETH: series(D0, ["10"] * 3)}
    result = replay_book(coins, plan, config(cost=Decimal(0)))
    assert result.days[0].traded == 2
    assert result.days[1].closed == 1


def test_trend_vol_planner_runs_the_real_rule():
    up = [str(100 + i + (i % 3)) for i in range(40)]
    down = [str(200 - i - (i % 2)) for i in range(40)]
    coins = {BTC: series(D0, up), ETH: series(D0, down)}
    params = PortfolioParams(
        lookback_days=5, vol_days=5, vol_target=Decimal("0.02"), gross_cap=Decimal(2), min_history_days=10
    )
    planner = trend_vol_planner(params, BookVolParams(Decimal("0.5"), 5, Decimal(2), Decimal("0.3")))
    start = first_ready_day(coins, 0, 10)
    assert start == D0 + timedelta(days=9)
    result = replay_book(coins, planner, ReplayConfig(0, start, D0 + timedelta(days=39)))
    assert result.days[0].traded == 2
    assert result.days[0].unit_vol is not None
    assert result.days[0].book_gross is not None
    assert not result.ruined
    assert result.halves[0].days + result.halves[1].days == result.full.days == 31


def test_first_ready_day_none_when_a_coin_is_short():
    assert first_ready_day({BTC: series(D0, ["1"] * 5)}, 0, 10) is None


def day(i, equity, gross="1", closed=0, btc=None):
    return DayResult(
        D0 + timedelta(days=i),
        0,
        Decimal(equity),
        Decimal(gross),
        Decimal(0),
        Decimal(0),
        True,
        0,
        0,
        0,
        0,
        0,
        closed,
        None,
        None,
        None if btc is None else Decimal(btc),
    )


def test_measures_known_series():
    days = [day(0, "110", closed=1), day(1, "99"), day(2, "108.9", gross="3")]
    m = measures(days, Decimal(100))
    assert m.end_multiple == Decimal("1.089")
    assert m.max_drawdown == Decimal("0.1")
    assert m.avg_gross == Decimal(5) / 3
    assert m.closed == 1
    returns = [Decimal("0.1"), Decimal("-0.1"), Decimal("0.1")]
    mean = sum(returns, Decimal(0)) / 3
    sd = (sum(((r - mean) ** 2 for r in returns), Decimal(0)) / 2).sqrt()
    assert m.sharpe is not None
    assert m.sortino is not None
    assert abs(m.sharpe - mean / sd * Decimal(365).sqrt()) < Decimal("1e-20")
    down = (Decimal("0.01") / 3).sqrt()
    assert abs(m.sortino - mean / down * Decimal(365).sqrt()) < Decimal("1e-20")
    assert m.calmar is not None
    assert m.calmar > 0


def test_measures_flat_has_no_ratios():
    m = measures([day(0, "100"), day(1, "100")], Decimal(100))
    assert (m.sharpe, m.sortino, m.calmar, m.max_drawdown) == (None, None, None, Decimal(0))


def test_correlation():
    xs = [Decimal(1), Decimal(2), Decimal(3)]
    assert correlation(xs, [Decimal(2), Decimal(4), Decimal(6)]) == 1
    assert correlation(xs, [Decimal(3), Decimal(2), Decimal(1)]) == -1
    assert correlation(xs, [Decimal(1)] * 3) is None
    with pytest.raises(ValueError, match="lengths differ"):
        correlation(xs, xs[:2])


def test_bad_config_refused():
    bads: list[dict[str, Any]] = [{"cut_hour": 24}, {"equity": Decimal(0)}, {"cost": Decimal(-1)}, {"keep_days": 1}]
    for bad in bads:
        with pytest.raises(ValueError, match="must be"):
            ReplayConfig(**{"cut_hour": 0, "start": D0, "end": D0, **bad})
    with pytest.raises(ValueError, match="is before start"):
        ReplayConfig(0, D0, D0 - timedelta(days=1))


def test_each_day_keeps_positions_and_band_gaps():
    # covers: spec 0033 AC-10a, the reference's sides and band edge distances for the parity compare.
    coins = {BTC: series(D0, ["100", "100", "125"]), ETH: series(D0, ["50", "50", "50"])}
    result = replay_book(coins, fixed({BTC: 1000, ETH: -500}, band=Decimal("0.3")), config(days=3))
    first, second, third = result.days
    assert first.positions == {BTC: Decimal("10.000"), ETH: Decimal("-10.000")}
    assert first.band_gaps == {BTC: Decimal(1), ETH: Decimal(1)}  # flat: the whole target away
    assert second.band_gaps == {BTC: Decimal(0), ETH: Decimal(0)}
    # Day 3: BTC at 125 holds 1250 against 1000, 25 % off, inside the 30 % band, so it is kept.
    assert third.band_gaps[BTC] == Decimal("0.25")
    assert third.positions[BTC] == Decimal("10.000")
    assert third.band == 2
