"""covers: spec 0014 AC-9 (four path points per minute, the side rule, trade sizes, the spread, quiet minutes)."""

from decimal import Decimal

import pytest

from trading_core.lab.candle_path import (
    FINAL_QUOTE_DELAY_NS,
    MS_NS,
    extremes,
    final_point,
    path_points,
    spread,
    trade_sizes,
)
from trading_core.lab.candles import MINUTE_NS, CandleRow

D = Decimal
M = 1_788_220_800_000_000_000  # 2026-09-01 00:00 UTC, a minute's open
TICK, STEP = D(1), D("0.0001")


def _candle(o, h, lo, c, v="1", quiet=False):
    return CandleRow(M + MINUTE_NS, D(o), D(h), D(lo), D(c), D(v), quiet)


def test_points_sit_at_open_extremes_and_close():
    points = path_points(_candle(100, 110, 95, 105), 0, D(0), TICK, STEP)
    assert [p.ts_ns - M for p in points] == [MS_NS, 20_000 * MS_NS, 40_000 * MS_NS, MINUTE_NS - MS_NS]
    assert [p.trade_id for p in points] == [f"{p.ts_ns}-{i}" for i, p in enumerate(points)]


@pytest.mark.parametrize(
    ("side", "candle", "expected"),
    [
        (1, (100, 110, 95, 105), (95, 110)),  # long: low first, stop first
        (-1, (100, 110, 95, 105), (110, 95)),  # short: high first
        (0, (100, 110, 95, 105), (95, 110)),  # flat: the low is nearer the open
        (0, (100, 104, 90, 95), (104, 90)),  # flat: the high is nearer
        (0, (100, 105, 95, 100), (105, 95)),  # flat tie: the high first
    ],
)
def test_the_extremes_follow_the_position(side, candle, expected):
    assert extremes(_candle(*candle), side) == tuple(D(x) for x in expected)


def test_trade_sizes_floor_a_quarter_and_never_go_below_the_step():
    assert trade_sizes(D("1.0003"), STEP) == [D("0.2500"), D("0.2500"), D("0.2500"), D("0.2503")]
    assert trade_sizes(D("0.0002"), STEP) == [STEP, STEP, STEP, STEP]
    assert trade_sizes(D(0), STEP) == [STEP, STEP, STEP, STEP]


def test_the_spread_rounds_away_from_the_price():
    assert spread(D(60000), D(2), TICK) == (D(59988), D(60012))
    assert spread(D(60001), D(2), TICK) == (D(59988), D(60014))  # 59988.9998 floored, 60013.0002 ceiled
    assert spread(D(60000), D(0), TICK) == (D(60000), D(60000))


def test_a_quiet_minute_is_one_quote_at_the_close_point():
    [point] = path_points(_candle(100, 100, 100, 100, "0", quiet=True), 1, D(2), TICK, STEP)
    assert point.ts_ns == M + MINUTE_NS - MS_NS
    assert point.trade_size is None
    assert (point.bid, point.ask) == (D(99), D(101))


def test_the_final_point_repeats_the_last_close_two_ms_later():
    last = path_points(_candle(100, 110, 95, 105), 0, D(2), TICK, STEP)[-1]
    final = final_point(last)
    assert final.ts_ns == last.ts_ns + FINAL_QUOTE_DELAY_NS
    assert (final.price, final.bid, final.ask, final.trade_size) == (last.price, last.bid, last.ask, None)
