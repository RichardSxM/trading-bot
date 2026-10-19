"""covers: spec 0015 AC-6 (daily closes per lab) and the measures run_measures stores."""

from datetime import UTC, date, datetime
from decimal import Decimal

from trading_core.lab.candle_metrics import EquityPoint
from trading_core.lab.daily import ClosedPosition, marked_daily, realized_daily, trade_measures
from trading_core.lab.metrics import FundingCash

HOUR = 3_600 * 1_000_000_000


def _ns(day: int, hour: int = 0) -> int:
    return int(datetime(2026, 1, day, hour, tzinfo=UTC).timestamp()) * 1_000_000_000


def _pos(long: bool, day: int, realized: str, fees: str = "1", funding: str = "0") -> ClosedPosition:
    return ClosedPosition(long, _ns(day, 12), Decimal(realized), Decimal(fees), Decimal(funding))


def test_marked_closes_take_the_midnight_mark_and_carry_a_gap_forward():
    curve = [EquityPoint(_ns(1, 12), Decimal(1005)), EquityPoint(_ns(2), Decimal(1010))]
    positions = [_pos(True, 1, "9")]
    funding = [FundingCash(_ns(1, 8), Decimal("-0.5"))]

    rows = marked_daily(curve, _ns(1), _ns(4), Decimal(1000), positions, funding)

    assert [(r.day, r.equity_close) for r in rows] == [
        (date(2026, 1, 1), Decimal(1010)),
        (date(2026, 1, 2), Decimal(1010)),
        (date(2026, 1, 3), Decimal(1010)),
    ]
    assert (rows[0].trades_closed, rows[0].fees, rows[0].funding) == (1, Decimal(1), Decimal("-0.5"))
    assert rows[1].trades_closed == 0


def test_replay_closes_are_the_balance_plus_that_days_net():
    positions = [_pos(True, 1, "5", funding="-1"), _pos(False, 3, "-2")]

    rows = realized_daily([date(2026, 1, 3), date(2026, 1, 1)], Decimal(1000), positions, [])

    assert [(r.day, r.equity_close) for r in rows] == [
        (date(2026, 1, 1), Decimal(1004)),
        (date(2026, 1, 3), Decimal(998)),
    ]


def test_trade_measures_split_sides_and_measure_edge_cost_and_concentration():
    positions = [
        _pos(True, 1, "10", fees="1", funding="-2"),
        _pos(False, 1, "-3", fees="1"),
        _pos(True, 2, "1", fees="1"),
    ]

    m = trade_measures(positions, Decimal("0.05"))

    assert (m.long_trades, m.long_net_pnl, m.short_trades, m.short_net_pnl) == (2, Decimal(9), 1, Decimal(-3))
    assert m.profit_factor == Decimal(9) / Decimal(3)
    assert m.expectancy == Decimal(2)
    # Top ceil(0.05 x 3) = 1 trade: 8 of the net 6.
    assert m.top_trades_profit_share == Decimal(8) / Decimal(6)
    # Gross = realized + fees: 11, -2, 2; cost = fees 3 plus funding paid 2.
    assert m.avg_gross_edge == Decimal(11) / Decimal(3)
    assert m.avg_round_trip_cost == Decimal(5) / Decimal(3)


def test_no_trades_leave_the_per_trade_measures_empty():
    m = trade_measures([], Decimal("0.05"))

    assert (m.expectancy, m.avg_gross_edge, m.avg_round_trip_cost, m.top_trades_profit_share) == (None,) * 4
