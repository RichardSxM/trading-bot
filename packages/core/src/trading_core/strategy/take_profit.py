"""A percent take profit, judged on the live quote: pure, so the node stays thin."""

from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import Side

_HUNDRED: Final = Decimal(100)


def take_profit_reached(side: Side, entry_px: Decimal, pct: Decimal, bid: Decimal, ask: Decimal) -> bool:
    """True when the price a close would get is at least ``pct`` percent better than ``entry_px``.

    A long closes on the bid and a short on the ask, so the spread cannot fake a win.
    """
    move = entry_px * pct / _HUNDRED
    if side == Side.LONG:
        return bid >= entry_px + move
    return ask <= entry_px - move
