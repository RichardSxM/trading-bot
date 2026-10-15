"""Entry sizing: a fixed notional (spec 0006 AC-4), and the risk size clipped to the caps (spec 0011 AC-1, AC-2)."""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Final

RISK: Final = "risk"
_BPS: Final = Decimal(10_000)


@dataclass(frozen=True)
class SizeBound:
    """One candidate size and what set it: ``risk``, or the reason of the cap that set it."""

    quantity: Decimal
    limited_by: str


def floor_to_step(value: Decimal, size_step: Decimal) -> Decimal:
    """``value`` rounded down to a whole number of ``size_step``; zero when below one step or negative.

    Raises ``ValueError`` when ``size_step`` is zero or below.
    """
    if size_step <= 0:
        raise ValueError(f"size step must be above zero, got {size_step}")
    if value <= 0:
        return Decimal(0).quantize(size_step)
    steps = (value / size_step).to_integral_value(rounding=ROUND_FLOOR)
    return (steps * size_step).quantize(size_step)


def entry_quantity(notional: Decimal, mid: Decimal, size_step: Decimal) -> Decimal:
    """``notional / mid`` rounded down to a whole number of ``size_step``; zero when below one step.

    Raises ``ValueError`` when any input is zero or below.
    """
    if notional <= 0 or mid <= 0 or size_step <= 0:
        raise ValueError(f"notional, mid, and size step must be above zero, got {notional}, {mid}, {size_step}")
    return floor_to_step(notional / mid, size_step)


def funding_cost_rate(cost_sign: int, rate: Decimal | None) -> Decimal:
    """The funding rate the trade would pay per period: ``max(0, cost_sign * rate)``, 0 when unknown.

    ``cost_sign`` is +1 for a buy (a long pays a positive rate) and -1 for a sell. A rate the trade
    would earn counts as 0, so expected income never makes a trade bigger.
    """
    if rate is None:
        return Decimal(0)
    return max(Decimal(0), cost_sign * rate)


def loss_per_unit(
    *,
    cost_sign: int,
    entry_px: Decimal,
    stop_px: Decimal,
    slippage_bps: Decimal,
    taker_fee: Decimal,
    funding_rate: Decimal | None,
    funding_periods: int,
) -> Decimal:
    """What one contract loses if the stop hits: distance, slippage past the stop, both fees, funding (AC-1)."""
    distance = abs(entry_px - stop_px)
    slippage = stop_px * slippage_bps / _BPS
    fees = taker_fee * (entry_px + stop_px)
    funding = funding_periods * funding_cost_rate(cost_sign, funding_rate) * entry_px
    return distance + slippage + fees + funding


def risk_quantity(equity: Decimal, risk_per_trade: Decimal, per_unit: Decimal, size_step: Decimal) -> Decimal:
    """``equity * risk_per_trade ÷ per_unit`` rounded down to the size step (AC-1).

    Raises ``ValueError`` when ``per_unit`` is zero or below.
    """
    if per_unit <= 0:
        raise ValueError(f"loss per unit must be above zero, got {per_unit}")
    return floor_to_step(equity * risk_per_trade / per_unit, size_step)


def smallest(caps: list[SizeBound], risk: SizeBound) -> SizeBound:
    """The smallest size (AC-2): on a tie a cap wins over ``risk``, and the first cap in ``caps`` wins."""
    best = caps[0]
    for bound in [*caps[1:], risk]:
        if bound.quantity < best.quantity:
            best = bound
    return best
