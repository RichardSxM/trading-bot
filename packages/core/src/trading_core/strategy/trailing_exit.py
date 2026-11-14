"""A dollar trailing stop and hard stop for one position, judged on the live quote: pure, so the nodes stay thin.

The open profit is read on the side of the book a close would get (a long on the bid, a short on the ask), so
the spread cannot fake a gain. Once the best open profit seen reaches ``arm_usd`` the trail is armed, and a
fall of ``trail_usd`` from that best closes the position as ``trailing_stop``. Before that, a loss of
``loss_usd`` closes it as ``stop_loss``. Fees are not counted.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.strategy.bar_rule import Side

TRAILING_STOP: Final = "trailing_stop"
STOP_LOSS: Final = "stop_loss"


@dataclass(frozen=True)
class TrailStep:
    """One quote's verdict: the open profit, the best open profit so far, and the exit reason or None."""

    pnl: Decimal
    peak: Decimal
    reason: str | None


def open_pnl(side: Side, entry_px: Decimal, quantity: Decimal, bid: Decimal, ask: Decimal) -> Decimal:
    """The position's open profit in USD if it closed now: a long on the bid, a short on the ask."""
    return (bid - entry_px) * quantity if side == Side.LONG else (entry_px - ask) * quantity


def trailing_exit(
    pnl: Decimal, peak: Decimal | None, arm_usd: Decimal, trail_usd: Decimal, loss_usd: Decimal
) -> TrailStep:
    """Judge one quote's open profit ``pnl`` against the best seen so far (``peak``, None before any quote).

    The hard stop at ``-loss_usd`` always applies. Once the new best reaches ``arm_usd``, a ``pnl`` at or
    below ``best - trail_usd`` exits as ``TRAILING_STOP``.
    """
    best = pnl if peak is None else max(peak, pnl)
    if pnl <= -loss_usd:
        return TrailStep(pnl, best, STOP_LOSS)
    if best >= arm_usd and pnl <= best - trail_usd:
        return TrailStep(pnl, best, TRAILING_STOP)
    return TrailStep(pnl, best, None)
