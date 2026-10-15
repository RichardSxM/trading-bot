"""Funding payments for replays (spec 0008 AC-9; our own recorded days from spec 0009 AC-14).

The engine applies no funding, so each unit samples, at every UTC funding boundary strictly
inside its segment, the mid and its signed position, and the parent turns the samples into
payments with the rates of the run's ``funding_rate_update`` rows. Kraken pays hourly.

An open position pays ``signed_qty * mid * rate`` (a long pays a positive rate, a short earns it),
so the amount booked to it is ``-signed_qty * mid * rate``.
"""

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.lab.errors import FundingRateMissingError
from trading_core.marketdata.hours import HOUR_NS

# Kraken's funding period, confirmed in the spec 0008 gate (`funding_timestamp` steps by 3600 s).
FUNDING_PERIOD_NS: Final = HOUR_NS


@dataclass(frozen=True, order=True)
class PositionKey:
    """A position as the recorder keys it: Nautilus's position id and when it opened.

    Netting reuses the position id after a close, so the open time tells them apart.
    """

    position_ref: str
    opened_ns: int


@dataclass(frozen=True)
class FundingSample:
    """The state at one funding boundary inside a segment: the mid, and the netted position.

    ``signed_qty`` is positive for a long, negative for a short, and 0 when flat (then
    ``position`` is ``None``).
    """

    boundary_ns: int
    mid: Decimal
    signed_qty: Decimal
    position: PositionKey | None


@dataclass(frozen=True)
class FundingRate:
    """One ``funding_rate_update``: the fractional rate per period from ``ts_ns`` on."""

    ts_ns: int
    rate: Decimal


@dataclass(frozen=True)
class FundingPayment:
    """One payment of one position: positive when it earned, negative when it paid."""

    boundary_ns: int
    position: PositionKey
    rate: Decimal
    amount: Decimal


def funding_amount(signed_qty: Decimal, mid: Decimal, rate: Decimal) -> Decimal:
    """What a position books at one boundary: ``-signed_qty * mid * rate`` (negative when it pays)."""
    return -signed_qty * mid * rate


def funding_boundaries(start_ns: int, end_ns: int) -> list[int]:
    """The funding boundaries strictly inside ``start_ns``..``end_ns`` (a segment is flat at both ends)."""
    first = start_ns - start_ns % FUNDING_PERIOD_NS + FUNDING_PERIOD_NS
    return list(range(first, end_ns, FUNDING_PERIOD_NS))


def rate_at(rates: Sequence[FundingRate], ts_ns: int) -> Decimal:
    """The rate of the last update at or before ``ts_ns``. ``rates`` must be sorted by ``ts_ns``.

    Raises ``FundingRateMissingError`` when no update comes at or before it.
    """
    index = bisect_right([r.ts_ns for r in rates], ts_ns)
    if index == 0:
        raise FundingRateMissingError(f"no funding_rate_update at or before {ts_ns}")
    return rates[index - 1].rate


def own_funding_gap(rates: Sequence[FundingRate], boundaries: Iterable[int]) -> int | None:
    """The first boundary our own recorded rates do not cover, or ``None`` when every one is covered.

    A boundary is covered when the last rate at or before it (as ``rate_at`` picks it) is less than
    one funding period old there: its ``ts_ns`` is in ``(boundary - FUNDING_PERIOD_NS, boundary]``.
    ``rates`` must be sorted by ``ts_ns`` (spec 0009 AC-14).
    """
    times = [r.ts_ns for r in rates]
    for boundary in sorted(boundaries):
        index = bisect_right(times, boundary)
        if index == 0 or times[index - 1] <= boundary - FUNDING_PERIOD_NS:
            return boundary
    return None


def apply_funding(samples: Iterable[FundingSample], rates: Iterable[FundingRate]) -> list[FundingPayment]:
    """One payment per sample with an open position, in boundary order.

    Raises ``FundingRateMissingError`` when a boundary has no rate at or before it, also when the
    position is flat there (the data must cover every boundary of the run).
    """
    ordered = sorted(rates, key=lambda r: r.ts_ns)
    payments: list[FundingPayment] = []
    for sample in sorted(samples, key=lambda s: s.boundary_ns):
        rate = rate_at(ordered, sample.boundary_ns)
        if sample.signed_qty == 0:
            continue
        if sample.position is None:
            raise ValueError(f"an open position at {sample.boundary_ns} has no position key")
        amount = funding_amount(sample.signed_qty, sample.mid, rate)
        payments.append(FundingPayment(sample.boundary_ns, sample.position, rate, amount))
    return payments
