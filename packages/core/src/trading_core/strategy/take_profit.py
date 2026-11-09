"""A dollar take profit and stop for one position, judged on the live quote: pure, so the nodes stay thin."""

from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import Side

TAKE_PROFIT: Final = "take_profit"
STOP_LOSS: Final = "stop_loss"


def dollar_exit(
    side: Side, entry_px: Decimal, quantity: Decimal, bid: Decimal, ask: Decimal, win_usd: Decimal, loss_usd: Decimal
) -> str | None:
    """``TAKE_PROFIT`` at ``win_usd`` or more of open profit, ``STOP_LOSS`` at ``loss_usd`` or more of open loss.

    A long closes on the bid and a short on the ask, so the spread cannot fake a win. Fees are not counted.
    """
    pnl = (bid - entry_px) * quantity if side == Side.LONG else (entry_px - ask) * quantity
    if pnl >= win_usd:
        return TAKE_PROFIT
    if pnl <= -loss_usd:
        return STOP_LOSS
    return None
