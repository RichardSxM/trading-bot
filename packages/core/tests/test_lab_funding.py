"""covers: spec 0008 AC-9 (funding payments at each UTC boundary inside a segment)."""

from decimal import Decimal

import pytest

from trading_core.lab.errors import FundingRateMissingError
from trading_core.lab.funding import (
    FundingRate,
    FundingSample,
    PositionKey,
    apply_funding,
    funding_boundaries,
    own_funding_gap,
    rate_at,
)
from trading_core.marketdata.hours import HOUR_NS

D = Decimal
T0 = 1_788_220_800_000_000_000  # 2026-09-01 00:00 UTC
LONG = PositionKey("PF_XBTUSD.KRAKEN-RandomTaker-S1", T0 + 10)


def test_boundaries_are_strictly_inside_the_segment():
    # A segment of three whole hours (end is the last nanosecond of the third).
    assert funding_boundaries(T0, T0 + 3 * HOUR_NS - 1) == [T0 + HOUR_NS, T0 + 2 * HOUR_NS]
    assert funding_boundaries(T0, T0 + HOUR_NS - 1) == []
    assert funding_boundaries(T0 + 5, T0 + HOUR_NS + 5) == [T0 + HOUR_NS]


def test_a_long_across_one_boundary_pays_qty_times_mid_times_rate():
    samples = [FundingSample(T0 + HOUR_NS, D("60000"), D("0.02"), LONG)]
    rates = [FundingRate(T0 + 30, D("0.0001"))]

    [payment] = apply_funding(samples, rates)

    assert payment.amount == -D("0.02") * D("60000") * D("0.0001")
    assert payment.amount == D("-0.12")
    assert (payment.position, payment.rate, payment.boundary_ns) == (LONG, D("0.0001"), T0 + HOUR_NS)


def test_a_short_earns_and_a_flat_boundary_pays_nothing():
    samples = [
        FundingSample(T0 + 2 * HOUR_NS, D("60000"), D("-0.01"), LONG),
        FundingSample(T0 + HOUR_NS, D("60000"), D("0"), None),
    ]

    [payment] = apply_funding(samples, [FundingRate(T0, D("0.0002"))])

    assert payment.amount == D("0.12")


def test_the_last_rate_at_or_before_the_boundary_counts():
    rates = [FundingRate(T0 + HOUR_NS + 1, D("0.5")), FundingRate(T0, D("0.1")), FundingRate(T0 + HOUR_NS, D("0.2"))]
    ordered = sorted(rates, key=lambda r: r.ts_ns)

    assert rate_at(ordered, T0 + HOUR_NS) == D("0.2")
    assert rate_at(ordered, T0 + HOUR_NS - 1) == D("0.1")


def test_a_boundary_without_an_earlier_rate_raises_even_when_flat():
    samples = [FundingSample(T0 + HOUR_NS, D("60000"), D("0"), None)]

    with pytest.raises(FundingRateMissingError):
        apply_funding(samples, [FundingRate(T0 + HOUR_NS + 1, D("0.1"))])


B = 10 * HOUR_NS


@pytest.mark.parametrize(
    ("rate_ts", "gap"),
    [
        ([B], None),  # a rate exactly at the boundary covers it
        ([B - HOUR_NS + 1], None),  # one ns less than a period old
        ([B - HOUR_NS], B),  # exactly one period old is too old
        ([B + 1], B),  # only a rate after the boundary
        ([], B),
        ([B - HOUR_NS + 1, B + 1], None),  # rate_at picks the one before
    ],
)
def test_own_funding_covers_a_boundary_only_with_a_rate_from_the_last_period(rate_ts, gap):
    """covers: spec 0009 AC-14."""
    rates = [FundingRate(ts, Decimal("0.0001")) for ts in rate_ts]

    assert own_funding_gap(rates, [B]) == gap


def test_own_funding_gap_names_the_first_uncovered_boundary():
    """covers: spec 0009 AC-14."""
    rates = [FundingRate(B - 10, Decimal(1)), FundingRate(B + 2 * HOUR_NS - 10, Decimal(1))]

    assert own_funding_gap(rates, [B + 2 * HOUR_NS, B + HOUR_NS, B]) == B + HOUR_NS
    assert own_funding_gap(rates, []) is None
