"""covers: spec 0014 AC-16 (marked equity, drawdown duration, exposure, profit factor, expectancy, benchmark),
spec 0022 AC-9 (the combined two coin curve, union exposure, mean benchmark)."""

from decimal import Decimal

from trading_core.lab.candle_metrics import (
    EquityPoint,
    MarkFill,
    TradeOutcome,
    benchmark,
    candle_measures,
    combined_equity,
    drawdown,
    exposure,
    marked_equity,
    mean_benchmark,
)
from trading_core.lab.metrics import FundingCash
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
H = HOUR_NS


def test_marked_equity_adds_open_pnl_fees_and_funding():
    fills = [MarkFill(H // 2, True, D(1), D(100), D("0.05")), MarkFill(3 * H + 1, False, D(1), D(130), D("0.065"))]
    funding = [FundingCash(2 * H, D("-0.01"))]
    marks = [(H, D(110)), (2 * H, D(90)), (3 * H, D(120)), (4 * H, D(130))]

    curve = marked_equity(D(1000), fills, funding, marks)

    assert [p.equity for p in curve] == [
        D(1000) - D("0.05") + 10,
        D(1000) - D("0.05") - 10 - D("0.01"),
        D(1000) - D("0.05") + 20 - D("0.01"),
        D(1000) - D("0.05") - D("0.065") + 30 - D("0.01"),  # closed: realized only
    ]


def test_a_short_then_a_flip_through_flat_marks_the_rest_at_its_own_price():
    fills = [MarkFill(1, False, D(1), D(100), D(0)), MarkFill(2, True, D(3), D(90), D(0))]
    [point] = marked_equity(D(0), fills, [], [(H, D(95))])
    # Short 1 at 100 closed at 90 (+10), then long 2 at 90 marked at 95 (+10).
    assert point.equity == D(20)


def test_drawdown_is_the_worst_fall_and_the_longest_time_under_a_peak():
    points = [EquityPoint(h * H, D(e)) for h, e in enumerate([100, 110, 99, 105, 111, 100, 100])]
    worst, hours = drawdown(points, D(100))
    assert worst == D(11) / D(110)
    assert hours == 3  # from the 110 peak at hour 1 until 111 at hour 4 (hour 4 to the end is only 2)


def test_exposure_profit_factor_expectancy_and_benchmark():
    trades = [
        TradeOutcome(True, 0, 2 * H, D(30)),
        TradeOutcome(False, 5 * H, 6 * H, D(-10)),
        TradeOutcome(True, 9 * H, None, D(0)),  # still open at the range end
    ]
    assert exposure(trades, 0, 10 * H) == D("0.4")
    measures = candle_measures(D(1000), [EquityPoint(H, D(1000))], trades, 0, 10 * H, D(100), D(110))
    assert measures.profit_factor == D(3)
    assert measures.expectancy == D(10)
    assert (measures.long.trades, measures.long.net_pnl, measures.short.trades) == (1, D(30), 1)
    assert measures.benchmark == D("0.1")
    no_loss = candle_measures(D(1000), [], trades[:1], 0, 10 * H, None, None)
    assert no_loss.profit_factor is None
    assert no_loss.benchmark is None
    assert benchmark(D(0), D(1)) is None


def test_two_coin_curves_combine_on_the_union_of_their_hour_closes():
    btc = [EquityPoint(1 * H, D(1010)), EquityPoint(2 * H, D(990)), EquityPoint(3 * H, D(1000))]
    eth = [EquityPoint(2 * H, D(1050)), EquityPoint(3 * H, D(1020))]  # ETH's first close comes an hour later
    combined = combined_equity(D(1000), [btc, eth])
    # 1000 + (coin equity - 1000) summed; ETH adds 0 before its first point.
    assert combined == [EquityPoint(1 * H, D(1010)), EquityPoint(2 * H, D(1040)), EquityPoint(3 * H, D(1020))]
    assert drawdown(combined, D(1000)) == (D(20) / D(1040), 1)
    assert combined_equity(D(1000), [btc]) == btc


def test_a_coin_without_a_point_at_a_time_carries_its_last_one():
    btc = [EquityPoint(1 * H, D(1010)), EquityPoint(3 * H, D(1030))]
    eth = [EquityPoint(1 * H, D(995)), EquityPoint(2 * H, D(985))]
    assert [p.equity for p in combined_equity(D(1000), [btc, eth])] == [D(1005), D(995), D(1015)]


def test_exposure_counts_overlapping_coins_once():
    trades = [
        TradeOutcome(True, 0, 4 * H, D(1)),  # BTC
        TradeOutcome(True, 2 * H, 6 * H, D(1)),  # ETH, open over BTC's last 2 hours
        TradeOutcome(False, 8 * H, None, D(0)),
    ]
    assert exposure(trades, 0, 10 * H) == D("0.8")


def test_the_multi_coin_benchmark_is_the_plain_mean_of_the_coins():
    assert mean_benchmark([(D(100), D(110)), (D(2000), D(1900))]) == D("0.025")  # (0.10 - 0.05) / 2
    assert mean_benchmark([(D(100), D(110)), (None, None)]) is None
    assert mean_benchmark([]) is None
