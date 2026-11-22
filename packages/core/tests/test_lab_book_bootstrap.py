"""covers: spec 0033 AC-14a, AC-15a, the bootstrap and benchmark helpers: pool runs never bridge a gap, blocks
stay in their run, paths are reproducible from the seed and cut to length, path prices and funding keys, nearest
rank percentiles, the hold and gross 1 planners, and return scaling.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_core.lab.book_bootstrap import (
    FUNDING_KEY_NS,
    CoinHistory,
    block_starts,
    bootstrap_paths,
    daily_closes,
    daily_funding,
    hold_planner,
    nearest_rank,
    path_coins,
    pool_runs,
    scaled_days,
    unit_gross,
)
from trading_core.lab.book_replay import (
    FILL_DELAY_NS,
    CoinData,
    CoinSpec,
    ReplayConfig,
    cut_ns,
    replay_book,
    trend_daily_planner,
)
from trading_core.marketdata.hours import HOUR_NS
from trading_core.strategy.trend_portfolio import DailyClose, PortfolioParams

D0 = date(2024, 1, 1)
STEP = CoinSpec(Decimal("0.001"))


def days(n, start=D0):
    return [start + timedelta(days=i) for i in range(n)]


def test_daily_closes_and_funding():
    data = CoinData(
        {cut_ns(D0, 0): Decimal(100), cut_ns(D0, 1): Decimal(101), cut_ns(D0 + timedelta(days=1), 0): Decimal(102)},
        {},
        {cut_ns(D0, 0) + HOUR_NS: Decimal("0.0001"), cut_ns(D0, 0) + 2 * HOUR_NS: Decimal("0.0002")},
        STEP,
    )
    assert daily_closes(data) == {D0: Decimal(100), D0 + timedelta(days=1): Decimal(102)}
    funding, missing = daily_funding(data, [D0, D0 + timedelta(days=1)])
    assert funding[D0 + timedelta(days=1)] == Decimal("0.0003")
    assert (funding[D0], missing) == (Decimal(0), 1)


def test_pool_runs_split_at_a_gap_and_blocks_stay_inside():
    a = {d: Decimal(1) for d in days(10)}
    b = dict(a)
    del b[D0 + timedelta(days=5)]  # day 5 has no close, so days 5 and 6 have no return
    runs = pool_runs({"a": a, "b": b}, D0, D0 + timedelta(days=9))
    assert runs == [days(4, D0 + timedelta(days=1)), days(3, D0 + timedelta(days=7))]
    assert block_starts(runs, 3) == [D0 + timedelta(days=1), D0 + timedelta(days=2), D0 + timedelta(days=7)]
    with pytest.raises(ValueError, match="no 5 day block"):
        bootstrap_paths(runs, 5, 10, 1, 7)


def test_paths_are_reproducible_and_cut_to_length():
    runs = [days(30)]
    one = bootstrap_paths(runs, 7, 20, 3, 7)
    assert one == bootstrap_paths(runs, 7, 20, 3, 7)
    assert all(len(p) == 20 for p in one)
    assert one != bootstrap_paths(runs, 7, 20, 3, 8)
    first = one[0]
    assert all(first[i + 1] - first[i] == timedelta(days=1) for i in range(6))  # a whole block of days


def test_path_coins_prices_fills_and_funding():
    closes = {D0: Decimal(100), D0 + timedelta(days=1): Decimal(110), D0 + timedelta(days=2): Decimal(99)}
    history = {"a": CoinHistory(closes, {D0 + timedelta(days=1): Decimal("0.001")}, STEP)}
    warmup = [D0 + timedelta(days=2)]  # the last real close is 99
    path = [date(2025, 1, 1), date(2025, 1, 2)]
    drawn = [D0 + timedelta(days=1), D0 + timedelta(days=2)]  # +10 %, then -10 %
    coin = path_coins(history, warmup, path, drawn)["a"]
    assert coin.hourly[cut_ns(path[0], 0)] == Decimal(99) * Decimal("1.1")
    assert coin.hourly[cut_ns(path[1], 0)] == Decimal(99) * Decimal("1.1") * Decimal("0.9")
    assert coin.minutes[cut_ns(path[1], 0) + FILL_DELAY_NS] == coin.hourly[cut_ns(path[1], 0)]
    assert coin.funding == {cut_ns(date(2024, 12, 31), 0) + FUNDING_KEY_NS: Decimal("0.001")}


def test_nearest_rank():
    values = [Decimal(i) for i in range(1, 1001)]
    assert nearest_rank(values, Decimal("0.95")) == Decimal(950)
    assert nearest_rank(values, Decimal("0.05")) == Decimal(50)
    assert nearest_rank([Decimal(3)], Decimal("0.5")) == Decimal(3)


def closes(n, start=100):
    return [DailyClose(D0 + timedelta(days=i), Decimal(start + i)) for i in range(n)]


def test_hold_planner_weights_the_ready_coins_equally():
    plan = hold_planner(5)({"a": closes(6), "b": closes(6), "c": closes(3)}, Decimal(1000))
    assert plan.plan.coins["a"].target_notional == Decimal(500)
    assert not plan.plan.coins["c"].eligible
    btc_only = hold_planner(5, ["a"])({"a": closes(6), "b": closes(6)}, Decimal(1000))
    assert btc_only.plan.coins["a"].weight == 1
    assert btc_only.plan.coins["b"].weight == 0


def test_unit_gross_divides_by_the_plan_gross():
    params = PortfolioParams(
        lookback_days=3, vol_days=3, vol_target=Decimal("0.02"), gross_cap=Decimal(2), min_history_days=5
    )
    series = {
        "a": [DailyClose(D0 + timedelta(days=i), Decimal(100 + i * i)) for i in range(8)],
        "b": [DailyClose(D0 + timedelta(days=i), Decimal(300 - i * 3 + (i % 2))) for i in range(8)],
    }
    plan = unit_gross(trend_daily_planner(params))(series, Decimal(1000))
    assert sum(abs(c.weight) for c in plan.plan.coins.values()) == 1
    assert plan.band == 0


def test_scaled_days_doubles_each_return():
    coins = {
        "a": CoinData(
            {cut_ns(d, 0): Decimal(p) for d, p in zip(days(3), (100, 110, 99), strict=True)},
            {cut_ns(d, 0) + FILL_DELAY_NS: Decimal(p) for d, p in zip(days(3), (100, 110, 99), strict=True)},
            {},
            STEP,
        )
    }
    result = replay_book(coins, hold_planner(1), ReplayConfig(0, D0, D0 + timedelta(days=2), cost=Decimal(0)))
    scaled = scaled_days(result.days, Decimal(10000), Decimal(2))
    assert scaled[1].equity == Decimal(10000) * (1 + (result.days[1].equity / result.days[0].equity - 1) * 2)
    assert scaled[1].gross == result.days[1].gross * 2
