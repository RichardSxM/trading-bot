"""The pure check before a rebalance order opens or grows a position (spec 0030 AC-5).

``check_rebalance`` applies the trader's ``RiskLimits`` to one wanted order: it refuses a stale quote,
clips the order to the per order notional cap, then so the book's gross exposure stays within the total
exposure cap and the leverage cap, and refuses when the resulting position's liquidation distance is under
the minimum. A reduce only order never comes here: reducing is always allowed. No I/O.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.recording.enums import OrderSide
from trading_core.risk.check import EXPOSURE_CAP, LEVERAGE_CAP, NOTIONAL_CAP, RiskLimits
from trading_core.risk.liquidation import distance_pct, liquidation_price, maintenance_rate
from trading_core.risk.sizing import floor_to_step

STALE_QUOTE: Final = "stale_quote"
LIQ_DISTANCE: Final = "liq_distance"


class InvalidRebalanceInput(ValueError):
    """The request cannot be judged (a mid, equity, or step at or below 0, or a negative wanted size)."""


@dataclass(frozen=True, kw_only=True)
class RebalanceRequest:
    """One opening or growing order: its side and wanted size, the quote, the book, and the coin's position.

    ``book_exposure`` is the sum of ``|quantity| x mid`` over every open position; ``current_qty`` is this
    coin's signed position before the order (0 for a flip's open leg, whose close already filled).
    """

    side: OrderSide
    quantity: Decimal
    mid: Decimal
    quote_age_s: Decimal
    equity: Decimal
    book_exposure: Decimal
    current_qty: Decimal
    size_step: Decimal
    instrument_maintenance_rate: Decimal
    stale_quote_s: Decimal


@dataclass(frozen=True)
class RebalanceVerdict:
    """Approved with ``quantity`` (clipped when ``clips`` is not empty), or refused with ``reason``."""

    approved: bool
    quantity: Decimal
    reason: str | None
    clips: tuple[str, ...]
    liq_px: Decimal | None
    liq_distance_pct: Decimal | None


def _validate(request: RebalanceRequest) -> None:
    if request.mid <= 0 or request.equity <= 0 or request.size_step <= 0:
        raise InvalidRebalanceInput(
            f"mid, equity, and size step must be above 0, got {request.mid}, {request.equity}, {request.size_step}"
        )
    if request.quantity <= 0 or request.book_exposure < 0 or request.quote_age_s < 0:
        raise InvalidRebalanceInput("wanted quantity must be above 0; exposure and quote age not negative")


def check_rebalance(request: RebalanceRequest, limits: RiskLimits) -> RebalanceVerdict:
    """Approve, clip, or refuse one opening or growing rebalance order (AC-5).

    In this order: ``stale_quote`` when the quote is older than ``stale_quote_s``; clips to
    ``notional_cap``, ``exposure_cap`` (book exposure plus the order within ``max_total_exposure``), and
    ``leverage_cap`` (the same within ``max_leverage x equity``), each rounded down to the size step; a
    clip that leaves less than one step refuses with that cap's name; ``liq_distance`` when the resulting
    position's liquidation price is nearer than ``min_liq_distance_pct``. Raises ``InvalidRebalanceInput``
    when the request cannot be judged.
    """
    _validate(request)
    if request.quote_age_s > request.stale_quote_s:
        return RebalanceVerdict(False, Decimal(0), STALE_QUOTE, (), None, None)
    mid, step = request.mid, request.size_step
    quantity = floor_to_step(request.quantity, step)
    clips: list[str] = []
    bounds = (
        (limits.max_order_notional / mid, NOTIONAL_CAP),
        ((limits.max_total_exposure - request.book_exposure) / mid, EXPOSURE_CAP),
        ((limits.max_leverage * request.equity - request.book_exposure) / mid, LEVERAGE_CAP),
    )
    for bound, name in bounds:
        capped = floor_to_step(bound, step)
        if capped < quantity:
            quantity = capped
            clips.append(name)
            if quantity <= 0:
                return RebalanceVerdict(False, Decimal(0), name, tuple(clips), None, None)
    rate = maintenance_rate(request.instrument_maintenance_rate, limits.maintenance_margin_rate)
    sign = 1 if request.side == OrderSide.BUY else -1
    position_qty = abs(request.current_qty + sign * quantity)
    other = max(Decimal(0), request.book_exposure - abs(request.current_qty) * mid)
    liq_px = liquidation_price(request.side, position_qty, mid, request.equity, other, Decimal(0), rate)
    liq_pct = distance_pct(mid, liq_px)
    if liq_pct is not None and liq_pct < limits.min_liq_distance_pct:
        return RebalanceVerdict(False, quantity, LIQ_DISTANCE, tuple(clips), liq_px, liq_pct)
    return RebalanceVerdict(True, quantity, None, tuple(clips), liq_px, liq_pct)
