"""A daily take profit and stop for a whole book, judged on the day's P&L: pure, so the node stays thin.

Both limits are a share of the book's planned size (the sum of every coin's target notional, without sign),
not of equity, so the same settings mean the same price move at any leverage.
"""

from decimal import Decimal
from typing import Final

BOOK_TAKE_PROFIT: Final = "book_take_profit"
BOOK_STOP_LOSS: Final = "book_stop_loss"
BOOK_EXIT_REASONS: Final = (BOOK_TAKE_PROFIT, BOOK_STOP_LOSS)


def book_notional(target_notionals: list[Decimal]) -> Decimal:
    """The book's planned size: the sum of the targets without their signs."""
    return sum((abs(target) for target in target_notionals), Decimal(0))


def book_exit(
    day_pnl: Decimal, notional: Decimal, take_profit_pct: Decimal | None, stop_loss_pct: Decimal | None
) -> str | None:
    """``BOOK_TAKE_PROFIT`` once the day's P&L is ``take_profit_pct`` of ``notional`` or more, ``BOOK_STOP_LOSS``
    once the day's loss is ``stop_loss_pct`` of it or more, else None.

    A limit that is None is off. A book with no planned size has nothing to close, so it gives None. Raises
    ``ValueError`` for a negative ``notional`` or a limit at or below 0.
    """
    if notional < 0:
        raise ValueError(f"notional must be at least 0, got {notional}")
    for limit in (take_profit_pct, stop_loss_pct):
        if limit is not None and limit <= 0:
            raise ValueError(f"a book exit limit must be above 0, got {limit}")
    if notional == 0:
        return None
    if take_profit_pct is not None and day_pnl >= take_profit_pct * notional:
        return BOOK_TAKE_PROFIT
    if stop_loss_pct is not None and day_pnl <= -stop_loss_pct * notional:
        return BOOK_STOP_LOSS
    return None
