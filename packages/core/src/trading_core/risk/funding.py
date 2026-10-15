"""Funding inside a planned hold, and the rules for booking it in demo (spec 0011 AC-9, AC-13)."""

from collections.abc import Iterable, Sequence
from decimal import Decimal

from trading_core.lab.errors import FundingRateMissingError
from trading_core.lab.funding import FUNDING_PERIOD_NS, FundingRate, rate_at


def funding_periods(now_ns: int, max_hold_ns: int | None) -> int:
    """The funding boundaries (top of each UTC hour) in ``(now_ns, now_ns + max_hold_ns]``.

    A strategy with no planned longest hold counts as 1 period. Raises ``ValueError`` on a negative hold.
    """
    if max_hold_ns is None:
        return 1
    if max_hold_ns < 0:
        raise ValueError(f"max hold must not be negative, got {max_hold_ns}")
    return (now_ns + max_hold_ns) // FUNDING_PERIOD_NS - now_ns // FUNDING_PERIOD_NS


def next_boundary_ns(now_ns: int) -> int:
    """The first funding boundary strictly after ``now_ns``."""
    return (now_ns // FUNDING_PERIOD_NS + 1) * FUNDING_PERIOD_NS


def charged(opened_ns: int, closed_ns: int | None, boundary_ns: int) -> bool:
    """A position pays at ``boundary_ns`` when it opened before it and was still open after it (AC-13).

    So a position that opens or closes exactly at a boundary is not charged for it.
    """
    return opened_ns < boundary_ns and (closed_ns is None or closed_ns > boundary_ns)


def qty_at(fills: Iterable[tuple[int, Decimal]], boundary_ns: int) -> Decimal:
    """The signed quantity at ``boundary_ns``: the sum of the signed fill quantities at or before it."""
    return sum((qty for ts_ns, qty in fills if ts_ns <= boundary_ns), Decimal(0))


def usable_rate(rates: Sequence[FundingRate], boundary_ns: int) -> Decimal | None:
    """The last rate at or before ``boundary_ns`` (as ``rate_at`` picks it), if it is under one period old there.

    ``rates`` must be sorted by ``ts_ns``. ``None`` when no rate qualifies.
    """
    try:
        rate = rate_at(rates, boundary_ns)
    except FundingRateMissingError:
        return None
    known_ns = max(r.ts_ns for r in rates if r.ts_ns <= boundary_ns)
    return rate if known_ns > boundary_ns - FUNDING_PERIOD_NS else None
