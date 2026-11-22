"""Funding for a book run through the candle lab (spec 0033 AC-19c).

The candle lab refuses a range with a charge time no stored rate covers (``carried_rate``, at most 3 h old). A
book study runs coins whose rates start long after the study window (Kraken stores most of them only from
2025-11-12), so a book run treats a charge time with no covering rate as the pure replay and the book mode do:
the coin pays nothing there, and every held coin at such a time is counted. One rate table per coin serves both
the engine account (``FundingModule``) and the bundle's payments, so the two agree.
"""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from trading_core.lab.candles import FundingRow, carried_rate
from trading_core.lab.funding import FundingPayment, FundingRate, FundingSample, apply_funding


def resolved_rates(rows: Sequence[FundingRow], times: Iterable[int]) -> dict[int, Decimal]:
    """Each charge time's rate as the candle lab carries it (``carried_rate``); a time with none is left out.

    ``rows`` must be sorted by charge time.
    """
    resolved: dict[int, Decimal] = {}
    for at in times:
        row = carried_rate(rows, at)
        if row is not None:
            resolved[at] = row.rate
    return resolved


def book_payments(samples: Iterable[FundingSample], rates: Mapping[int, Decimal]) -> tuple[list[FundingPayment], int]:
    """The payments at the charge times that have a rate, and how many held samples had none (they pay nothing).

    A flat sample never counts as missing. Each payment uses the rate of its own charge time, never a later
    or earlier one.
    """
    kept: list[FundingSample] = []
    missing = 0
    for sample in samples:
        if sample.boundary_ns in rates:
            kept.append(sample)
        elif sample.signed_qty != 0:
            missing += 1
    table = [FundingRate(at, rate) for at, rate in sorted(rates.items())]
    return apply_funding(kept, table), missing
