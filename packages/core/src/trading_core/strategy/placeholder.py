"""The placeholder strategy's pure rules (spec 0006 AC-3, AC-6, AC-19).

It enters on a fixed beat (every ``PLACEHOLDER_INTERVAL_MIN`` minutes on the UTC clock), on
the side of a simple lookback move, protects the position with a stop a fixed percent away,
and closes on time. It is a walking skeleton, not meant to make money.
"""

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from trading_core.recording.enums import Intent, OrderSide, OrderType, PositionSide

NS_PER_MIN: Final = 60 * 1_000_000_000
# The decision fires this long after each boundary, so the boundary's bar has closed.
DECISION_DELAY_NS: Final = 2 * 1_000_000_000


class ExitReason(StrEnum):
    """Why a round trip closed, as the Trades page shows it."""

    STOP = "stop"
    TIME = "time"
    STOP_FAILURE = "stop_failure"
    ABANDONED = "abandoned"


def next_boundary_ns(now_ns: int, interval_min: int) -> int:
    """The first UTC boundary (a whole multiple of ``interval_min`` minutes) strictly after ``now_ns``."""
    if interval_min <= 0:
        raise ValueError(f"interval must be above zero, got {interval_min}")
    step = interval_min * NS_PER_MIN
    return (now_ns // step + 1) * step


def entry_side(closes: Sequence[Decimal], lookback: int) -> OrderSide | None:
    """``buy`` when the newest close is at or above the close ``lookback`` bars earlier, else ``sell``.

    ``closes`` runs oldest to newest. Returns ``None`` (warmup) with fewer than ``lookback + 1`` closes.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be at least 1, got {lookback}")
    if len(closes) < lookback + 1:
        return None
    return OrderSide.BUY if closes[-1] >= closes[-1 - lookback] else OrderSide.SELL


def stop_price(avg_px: Decimal, side: PositionSide, pct: Decimal, tick: Decimal) -> Decimal:
    """The protective stop trigger, ``pct`` percent from ``avg_px``, rounded away from the market to ``tick``.

    Below the entry for a long (rounded down), above for a short (rounded up).
    """
    if avg_px <= 0 or tick <= 0 or not 0 < pct < 100:
        raise ValueError(f"bad stop inputs: avg_px {avg_px}, pct {pct}, tick {tick}")
    if side == PositionSide.LONG:
        raw, rounding = avg_px * (1 - pct / 100), ROUND_FLOOR
    elif side == PositionSide.SHORT:
        raw, rounding = avg_px * (1 + pct / 100), ROUND_CEILING
    else:
        raise ValueError("a flat position has no stop")
    return ((raw / tick).to_integral_value(rounding=rounding) * tick).quantize(tick)


def exit_reason(abandoned: bool, last_fill_order_type: OrderType | None, decision_intent: Intent | None) -> ExitReason:
    """Why a round trip closed, from how its last fill came about.

    Abandoned at restart wins. Otherwise a stop market fill is ``stop``; a market fill carrying
    an ``exit`` decision is ``time``; any other market fill (it carries the entry's decision, the
    exit loop after a failed stop) is ``stop_failure``.
    """
    if abandoned:
        return ExitReason.ABANDONED
    if last_fill_order_type == OrderType.STOP_MARKET:
        return ExitReason.STOP
    if decision_intent == Intent.EXIT:
        return ExitReason.TIME
    return ExitReason.STOP_FAILURE
