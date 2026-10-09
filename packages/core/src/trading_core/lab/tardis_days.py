"""Which Tardis days an import may fetch, and whether the next one fits under the cap (spec 0008 AC-1, AC-2)."""

from datetime import date
from decimal import Decimal

from trading_core.lab.segments import is_first_of_month, required_days
from trading_core.marketdata.hours import GB

FREE_DAYS_ONLY = "free days are the first of each month only"


def import_days(first: date, last: date) -> list[date]:
    """The first of month days from ``first`` to ``last`` (both inclusive), oldest first.

    Both ends must be first of month days, because Tardis gives only those away; anything else is
    refused with ``ValueError`` before anything downloads. Also raises when ``first`` is after
    ``last``.
    """
    bad = [day.isoformat() for day in (first, last) if not is_first_of_month(day)]
    if bad:
        raise ValueError(f"{FREE_DAYS_ONLY}: {', '.join(dict.fromkeys(bad))}")
    return required_days("tardis", first, last)


def fits_under_cap(used_bytes: int, day_gb: Decimal, cap_gb: Decimal) -> bool:
    """Whether everything under the lab catalog plus one more day stays at or under the cap."""
    return Decimal(used_bytes) + day_gb * GB <= cap_gb * GB
