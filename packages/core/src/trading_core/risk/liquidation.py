"""The cross margin liquidation estimate (spec 0011 AC-5, AC-6).

The whole account backs the position: the exchange force closes it once equity falls to the
maintenance margin of everything open. The other positions' notional is held fixed and the exit
fee is ignored, so the estimate is exact for one instrument and an approximation for several.
"""

from decimal import Decimal

from trading_core.recording.enums import OrderSide


def maintenance_rate(instrument_rate: Decimal | None, floor: Decimal) -> Decimal:
    """The larger of the instrument's ``margin_maint`` (0 when unset) and the configured floor (AC-6)."""
    return max(instrument_rate or Decimal(0), floor)


def liquidation_price(
    side: OrderSide,
    quantity: Decimal,
    entry_px: Decimal,
    equity: Decimal,
    other_notional: Decimal,
    fee: Decimal,
    rate: Decimal,
) -> Decimal | None:
    """The price at which the account would be liquidated after this position opens at ``entry_px`` (AC-5).

    ``other_notional`` is the notional of every other open position, ``fee`` the entry fee in USD,
    and ``rate`` the maintenance rate. ``None`` when a long has no liquidation price (the estimate is
    zero or below). Raises ``ValueError`` when ``quantity`` is zero or below or ``rate`` is outside (0, 1).
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be above zero, got {quantity}")
    if not 0 < rate < 1:
        raise ValueError(f"maintenance rate must be in (0, 1), got {rate}")
    notional = quantity * entry_px
    if side == OrderSide.BUY:
        price = (notional + fee + rate * other_notional - equity) / (quantity * (1 - rate))
        return price if price > 0 else None
    return (equity + notional - fee - rate * other_notional) / (quantity * (1 + rate))


def distance_pct(entry_px: Decimal, liq_px: Decimal | None) -> Decimal | None:
    """``|entry_px - liq_px| / entry_px`` in percent; ``None`` when there is no liquidation price."""
    if liq_px is None:
        return None
    return abs(entry_px - liq_px) / entry_px * 100
