"""Entry sizing (spec 0006 AC-4): a fixed notional at the mid, rounded down to the size step."""

from decimal import ROUND_FLOOR, Decimal


def entry_quantity(notional: Decimal, mid: Decimal, size_step: Decimal) -> Decimal:
    """``notional / mid`` rounded down to a whole number of ``size_step``; zero when below one step.

    Raises ``ValueError`` when any input is zero or below.
    """
    if notional <= 0 or mid <= 0 or size_step <= 0:
        raise ValueError(f"notional, mid, and size step must be above zero, got {notional}, {mid}, {size_step}")
    steps = (notional / mid / size_step).to_integral_value(rounding=ROUND_FLOOR)
    return (steps * size_step).quantize(size_step)
