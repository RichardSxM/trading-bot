"""covers: spec 0011 AC-13 (who is charged, the rate and quantity at a boundary) and AC-16 (funding in the
loss tally)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from trading_core.lab.funding import FundingRate, funding_amount
from trading_core.risk.funding import charged, next_boundary_ns, qty_at, usable_rate
from trading_core.risk.limits import LossLimits, LossTally, evaluate, on_funding

HOUR_NS = 3_600 * 1_000_000_000
B = 10 * HOUR_NS  # a boundary
POSITION = UUID("0199d7a4-6d2f-7c4e-9a7b-0f1e2d3c4b5a")
LIMITS = LossLimits(
    daily_usd=Decimal(20), total_usd=Decimal(100), total_since=date(2026, 10, 1), max_trades_per_day=50, cooldown_s=0
)
TODAY = date(2026, 10, 12)
NOON = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("opened", "closed", "expected"),
    [
        (B - 1, None, True),
        (B - 1, B + 1, True),
        (B, None, False),  # opens exactly at the boundary
        (B - 1, B, False),  # closes exactly at the boundary
        (B + 1, None, False),
        (B - 10, B - 1, False),
    ],
)
def test_who_is_charged(opened, closed, expected):
    assert charged(opened, closed, B) is expected


def test_the_quantity_at_the_boundary_counts_fills_up_to_it():
    fills = [(B - 5, Decimal("0.003")), (B - 1, Decimal("-0.001")), (B + 1, Decimal("-0.002"))]
    assert qty_at(fills, B) == Decimal("0.002")


def test_the_rate_is_the_last_one_at_or_before_the_boundary_and_under_an_hour_old():
    rates = [
        FundingRate(B - HOUR_NS, Decimal("0.1")),
        FundingRate(B - 10, Decimal("0.2")),
        FundingRate(B + 1, Decimal(9)),
    ]
    assert usable_rate(rates, B) == Decimal("0.2")
    assert usable_rate([FundingRate(B - HOUR_NS, Decimal("0.1"))], B) is None  # exactly one period old
    assert usable_rate([FundingRate(B + 1, Decimal("0.1"))], B) is None
    assert usable_rate([], B) is None


def test_the_next_boundary_is_strictly_after_now():
    assert next_boundary_ns(B) == B + HOUR_NS
    assert next_boundary_ns(B - 1) == B


def test_a_long_pays_a_positive_rate():
    assert funding_amount(Decimal("0.002"), Decimal(100_000), Decimal("0.0001")) == Decimal("-0.02")
    assert funding_amount(Decimal("-0.002"), Decimal(100_000), Decimal("0.0001")) == Decimal("0.02")


def test_a_payment_today_can_trip_the_daily_limit_while_flat():
    tally = LossTally(day=TODAY, realized_today=Decimal(-19), realized_total=Decimal(-19))
    key = ("PF_XBTUSD.KRAKEN", B, POSITION)

    tally = on_funding(tally, key, Decimal("-1.5"), NOON, LIMITS)

    assert (tally.realized_today, tally.realized_total) == (Decimal("-20.5"), Decimal("-20.5"))
    assert evaluate(tally, Decimal(0), LIMITS, NOON).trip is not None


def test_a_payment_counts_once():
    key = ("PF_XBTUSD.KRAKEN", B, POSITION)
    once = on_funding(LossTally(day=TODAY), key, Decimal(-1), NOON, LIMITS)
    assert on_funding(once, key, Decimal(-1), NOON, LIMITS) == once


def test_a_payment_before_the_total_start_counts_nowhere():
    paid = datetime(2026, 9, 30, 12, tzinfo=UTC)
    tally = on_funding(LossTally(day=TODAY), ("X", B, POSITION), Decimal(-1), paid, LIMITS)
    assert (tally.realized_today, tally.realized_total) == (Decimal(0), Decimal(0))
