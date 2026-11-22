"""covers: spec 0033 AC-19c (a book run's funding: a charge time with no covering rate pays nothing and is
counted when a coin is held there; the rate table is the candle lab's carried rate)."""

from decimal import Decimal as D

from trading_core.lab.book_funding import book_payments, resolved_rates
from trading_core.lab.candles import FundingRow
from trading_core.lab.funding import FundingSample, PositionKey
from trading_core.marketdata.hours import HOUR_NS

T0 = 1_700_000_000 // 3600 * 3600 * 1_000_000_000
KEY = PositionKey("P-1", T0 - HOUR_NS)


def _row(at, rate):
    return FundingRow(at, D(rate), HOUR_NS, "api")


def test_a_rate_is_carried_up_to_three_hours_and_no_further():
    rows = [_row(T0, "0.0001")]
    times = [T0 + i * HOUR_NS for i in range(5)]

    assert resolved_rates(rows, times) == {
        T0: D("0.0001"),
        T0 + HOUR_NS: D("0.0001"),
        T0 + 2 * HOUR_NS: D("0.0001"),
        T0 + 3 * HOUR_NS: D("0.0001"),
    }


def test_no_rows_give_no_rates():
    assert resolved_rates([], [T0, T0 + HOUR_NS]) == {}


def test_a_held_sample_without_a_rate_pays_nothing_and_is_counted():
    samples = [
        FundingSample(T0, D(100), D(2), KEY),
        FundingSample(T0 + HOUR_NS, D(110), D(2), KEY),
        FundingSample(T0 + 2 * HOUR_NS, D(120), D(0), None),
    ]

    payments, missing = book_payments(samples, {T0: D("0.001")})

    assert missing == 1  # the flat sample with no rate never counts
    assert [(p.boundary_ns, p.amount) for p in payments] == [(T0, D("-0.200"))]


def test_each_payment_takes_its_own_charge_time_rate():
    samples = [FundingSample(T0, D(100), D(-1), KEY), FundingSample(T0 + HOUR_NS, D(100), D(-1), KEY)]

    payments, missing = book_payments(samples, {T0: D("0.001"), T0 + HOUR_NS: D("-0.002")})

    assert missing == 0
    assert [p.amount for p in payments] == [D("0.100"), D("-0.200")]
