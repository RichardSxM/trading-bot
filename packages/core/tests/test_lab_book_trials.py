"""covers: spec 0033 AC-12, AC-12a, AC-12b, the pre check trials: the hourly trial replay matches replay_book with
no rules, EWMA vols, tranches, scale in, the stop (first fill arms it, stop before the decision, retry on a missing
fill, flat until the sign changes), and the fill delay setting.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_core.lab.book_replay import (
    FILL_DELAY_NS,
    CoinData,
    CoinSpec,
    Planned,
    ReplayConfig,
    cut_ns,
    replay_book,
    trend_vol_planner,
)
from trading_core.lab.book_trials import (
    ScaleStop,
    ewma_planner,
    ewma_variance,
    ewma_vol,
    replay_trial,
    tranche_hours,
)
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import CoinPlan, PortfolioParams, PortfolioPlan
from trading_core.strategy.trend_vol import BookVolParams

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
D0 = date(2024, 1, 1)
STEP = CoinSpec(Decimal("0.001"))


def hourly_coin(first: date, prices, delay=FILL_DELAY_NS, funding=None, drop_fills=()):
    """A coin with a close at every whole hour from ``first`` (``prices`` one per hour, as strings) and a fill
    close ``delay`` after each hour at the same price, minus the fill times in ``drop_fills``."""
    hourly, minutes = {}, {}
    start = cut_ns(first, 0)
    for i, price in enumerate(prices):
        ns = start + i * HOUR_NS
        hourly[ns] = Decimal(price)
        if ns + delay not in drop_fills:
            minutes[ns + delay] = Decimal(price)
    return CoinData(hourly, minutes, funding or {}, STEP)


def flat_prices(days, price="100"):
    return [price] * (24 * days)


def fixed(targets, band=Decimal(0), vol=Decimal("0.01")):
    """A planner that targets ``targets`` (signed USD, or a callable of the call number); weights follow."""
    calls = {"n": 0}

    def plan(closes, equity):
        calls["n"] += 1
        wanted = targets(calls["n"]) if callable(targets) else targets
        coins = {}
        for coin, series in closes.items():
            target = Decimal(wanted.get(coin, 0))
            coins[coin] = CoinPlan(True, 1, vol, target / equity, target, series[-1].day if series else None)
        return Planned(PortfolioPlan(coins, len(coins), Decimal(1), Decimal(1), equity, None), band, None, Decimal(1))

    return plan


def config(days=3, **kw):
    return ReplayConfig(cut_hour=kw.pop("cut_hour", 0), start=D0, end=D0 + timedelta(days=days - 1), **kw)


def wavy(days, base, step, swing):
    return [str(base + step * (i // 24) + (i % 7) * swing) for i in range(24 * days)]


def test_no_rules_matches_replay_book_exactly():
    # The real trend_vol rule on two coins with funding: every day's figures must equal replay_book's.
    rates = {cut_ns(D0, 0) + i * HOUR_NS: Decimal("0.00001") for i in range(24 * 60)}
    coins = {BTC: hourly_coin(D0, wavy(60, 100, 1, 0.3), funding=rates), ETH: hourly_coin(D0, wavy(60, 300, -1, 0.5))}
    params = PortfolioParams(
        lookback_days=5, vol_days=5, vol_target=Decimal("0.02"), gross_cap=Decimal(2), min_history_days=10
    )
    planner = trend_vol_planner(params, BookVolParams(Decimal("0.5"), 5, Decimal(2), Decimal("0.3")))
    cfg = ReplayConfig(0, D0 + timedelta(days=10), D0 + timedelta(days=58), keep_days=30)
    base = replay_book(coins, planner, cfg)
    trial = replay_trial(coins, planner, cfg)
    assert trial.replay.days == base.days
    assert trial.replay.full == base.full
    assert trial.replay.halves == base.halves
    assert trial.replay.funding_missing_days == base.funding_missing_days
    assert trial.stops == 0
    assert trial.avg_turnover > 0


def test_fill_delay_setting():
    coins = {BTC: hourly_coin(D0, flat_prices(3), delay=HOUR_NS)}
    result = replay_book(coins, fixed({BTC: "1000"}), config(fill_delay_ns=HOUR_NS))
    assert result.days[0].fill_ns == cut_ns(D0, 0) + HOUR_NS
    assert result.days[0].traded == 1
    with pytest.raises(ValueError, match="fill_delay_ns"):
        config(fill_delay_ns=0)
    with pytest.raises(ValueError, match="fill_delay_ns"):
        config(fill_delay_ns=2 * HOUR_NS)


def test_ewma_variance_seed_and_update():
    returns = [Decimal("0.01"), Decimal("-0.01")] * 15 + [Decimal("0.02")]
    seeded = ewma_variance(returns[:30], 20)
    mean = Decimal(0)
    assert seeded == sum(((r - mean) ** 2 for r in returns[:30]), Decimal(0)) / 29
    lam = Decimal("0.5") ** (Decimal(1) / Decimal(20))
    assert abs(ewma_variance(returns, 20) - (lam * seeded + (1 - lam) * Decimal("0.0004"))) < Decimal("1e-20")
    with pytest.raises(ValueError, match="seed"):
        ewma_variance(returns[:29], 20)
    with pytest.raises(ValueError, match="half_life"):
        ewma_variance(returns, 0)
    assert ewma_vol([Decimal(100)] * 40, 10) == 0


def test_ewma_planner_runs_and_sizes_the_book():
    closes_up = [str(100 + i + (i % 3)) for i in range(60)]
    closes_down = [str(200 - i - (i % 2)) for i in range(60)]
    coins = {
        BTC: CoinData(
            {cut_ns(D0 + timedelta(days=i), 0): Decimal(c) for i, c in enumerate(closes_up)},
            {cut_ns(D0 + timedelta(days=i), 0) + FILL_DELAY_NS: Decimal(c) for i, c in enumerate(closes_up)},
            {},
            STEP,
        ),
        ETH: CoinData(
            {cut_ns(D0 + timedelta(days=i), 0): Decimal(c) for i, c in enumerate(closes_down)},
            {cut_ns(D0 + timedelta(days=i), 0) + FILL_DELAY_NS: Decimal(c) for i, c in enumerate(closes_down)},
            {},
            STEP,
        ),
    }
    params = PortfolioParams(
        lookback_days=5, vol_days=5, vol_target=Decimal("0.02"), gross_cap=Decimal(2), min_history_days=40
    )
    planner = ewma_planner(params, BookVolParams(Decimal("0.5"), 5, Decimal(2), Decimal("0.3")), 20)
    result = replay_book(coins, planner, ReplayConfig(0, D0 + timedelta(days=40), D0 + timedelta(days=59)))
    first = result.days[0]
    assert first.traded == 2
    assert first.unit_vol is not None
    assert first.unit_vol > 0
    assert first.positions[BTC] > 0 > first.positions[ETH]


def test_tranche_hours():
    assert tranche_hours(0, 4) == [0, 6, 12, 18]
    assert tranche_hours(5, 4) == [5, 11, 17, 23]
    assert tranche_hours(0, 1) == [0]
    with pytest.raises(ValueError, match="divide"):
        tranche_hours(0, 5)
    with pytest.raises(ValueError, match="passes"):
        tranche_hours(6, 4)


def test_tranches_trade_the_summed_target_at_every_cut():
    # Two sub books at 00 and 12, each wanting 1000 USD of BTC at the full target: the book holds 500 after the
    # first cut (one sub book planned, 1/2 of equity) and 1000 after the second.
    coins = {BTC: hourly_coin(D0, flat_prices(3))}
    equity = Decimal(10000)
    result = replay_trial(coins, fixed({BTC: "1000"}), config(cost=Decimal(0)), tranches=2)
    assert result.replay.days[0].positions[BTC] == Decimal(5)
    assert result.replay.days[1].positions[BTC] == Decimal(10)
    assert result.replay.days[1].traded == 1  # the 12:00 cut of day 1 is recorded with day 2
    assert result.replay.full.end_multiple == equity / equity


def test_rules_need_one_book():
    with pytest.raises(ValueError, match="one book"):
        replay_trial(
            {BTC: hourly_coin(D0, flat_prices(2))}, fixed({BTC: "900"}), config(days=2), tranches=2, rules=ScaleStop(3)
        )


def test_scale_in_over_n_days_with_the_band_off():
    coins = {BTC: hourly_coin(D0, flat_prices(5))}
    result = replay_trial(
        coins, fixed({BTC: "900"}, band=Decimal("0.5")), config(days=5, cost=Decimal(0)), rules=ScaleStop(3)
    )
    assert [d.positions[BTC] for d in result.replay.days] == [
        Decimal(3),
        Decimal(6),
        Decimal(9),
        Decimal(9),
        Decimal(9),
    ]
    assert [d.traded for d in result.replay.days] == [1, 1, 1, 0, 0]  # day 4 on target, band back on


def test_flip_restarts_the_ramp():
    targets = {1: {BTC: "900"}, 2: {BTC: "900"}, 3: {BTC: "-900"}, 4: {BTC: "-900"}}
    coins = {BTC: hourly_coin(D0, flat_prices(4))}
    result = replay_trial(coins, fixed(lambda n: targets[n]), config(days=4, cost=Decimal(0)), rules=ScaleStop(3))
    assert [d.positions[BTC] for d in result.replay.days] == [Decimal(3), Decimal(6), Decimal(-3), Decimal(-6)]


def stop_prices():
    """Day 1 at 100; day 2 falls to 90 at 10:00 and stays there."""
    prices = flat_prices(1) + ["100"] * 10 + ["90"] * 14 + ["90"] * 48
    return prices


def test_stop_closes_at_the_next_fill_and_stays_flat_until_the_sign_changes():
    # Long 10 BTC at 100, entry vol 1 %, stop at 8 vols: 92 is the line, the 90 close at day 2 10:00 trips it.
    coins = {BTC: hourly_coin(D0, stop_prices())}
    result = replay_trial(coins, fixed({BTC: "1000"}), config(days=4, cost=Decimal(0)), rules=ScaleStop(1, Decimal(8)))
    days = result.replay.days
    assert days[0].positions[BTC] == Decimal(10)
    assert days[1].positions[BTC] == Decimal(10)  # the stop is at 10:05 on day 2, after day 2's record
    assert days[2].positions[BTC] == 0  # still long signal, stays flat
    assert days[2].closed == 1
    assert days[3].positions[BTC] == 0
    assert result.stops == 1
    assert result.stopped_share == Decimal(2) / Decimal(4)


def test_stop_rearms_after_the_signal_changes():
    targets = {1: {BTC: "1000"}, 2: {BTC: "1000"}, 3: {BTC: "0"}, 4: {BTC: "1000"}}
    coins = {BTC: hourly_coin(D0, stop_prices())}
    result = replay_trial(
        coins, fixed(lambda n: targets[n]), config(days=4, cost=Decimal(0)), rules=ScaleStop(1, Decimal(8))
    )
    # Day 3's flat signal clears the stop; day 4 enters again at 90 (11.111 coins on the 0.001 step).
    assert result.replay.days[3].positions[BTC] == Decimal("11.111")


def test_stop_runs_before_the_decision_at_the_cut_hour():
    # The drop lands exactly on day 2's 00:00 close: the stop fills at 00:05 and the decision skips the coin.
    prices = flat_prices(1) + ["90"] * 48
    coins = {BTC: hourly_coin(D0, prices)}
    result = replay_trial(coins, fixed({BTC: "1000"}), config(days=3, cost=Decimal(0)), rules=ScaleStop(1, Decimal(8)))
    day2 = result.replay.days[1]
    assert day2.positions[BTC] == 0
    assert (day2.traded, day2.closed) == (1, 1)


def test_no_stop_before_the_first_fill():
    # Day 1 has no fill, so no entry price: the drop on day 1 cannot stop anything.
    prices = ["100"] * 10 + ["50"] * 62
    coins = {BTC: hourly_coin(D0, prices, drop_fills={cut_ns(D0, 0) + FILL_DELAY_NS})}
    result = replay_trial(coins, fixed({BTC: "1000"}), config(days=3, cost=Decimal(0)), rules=ScaleStop(1, Decimal(8)))
    assert result.replay.days[0].no_fill == 1
    assert result.stops == 0
    assert result.replay.days[1].positions[BTC] == Decimal(20)  # entered on day 2 at 50


def test_missing_stop_fill_retries_the_next_hour():
    day2_10 = cut_ns(D0 + timedelta(days=1), 10) + FILL_DELAY_NS
    coins = {BTC: hourly_coin(D0, stop_prices(), drop_fills={day2_10})}
    result = replay_trial(coins, fixed({BTC: "1000"}), config(days=3, cost=Decimal(0)), rules=ScaleStop(1, Decimal(8)))
    assert result.stops == 1
    assert result.replay.days[2].no_fill == 1
    assert result.replay.days[2].positions[BTC] == 0
