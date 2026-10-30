"""Fill recovery for local only orders (spec 0023 AC-11), and the untrusted cache key (AC-8).

An order open in the cache that the exchange no longer has most often filled while the trader
was not listening. ``recover`` matches the exchange's fills to each such order, by venue order id
when both sides have one, else by client order id (the adapter's own rule), skipping fills the
cache already holds (by trade id). An order with new fills gets a ``Recovery``: the fills to feed
Nautilus in venue time order, and whether they fill it (``FILLED``) or not (``CANCELED`` with
the filled quantity, since the exchange no longer has it). An order with none is not recovered;
the check marks it canceled.

``window_complete`` says whether a fetch of the exchange's last ``page`` fills covers the window:
a full page whose oldest fill is newer than the window's start may have left fills out.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.reconcile.views import FillView, OrderView

# Kraken Futures' ``/fills`` returns at most this many fills, the newest first, and the 1.231.0
# adapter asks for one page only.
KRAKEN_FILLS_PAGE: Final = 100


@dataclass(frozen=True, kw_only=True)
class Recovery:
    """A local only order's missed fills, oldest first, and its filled quantity once they land."""

    order: OrderView
    fills: tuple[FillView, ...]
    filled_qty: Decimal

    @property
    def complete(self) -> bool:
        """True when the fills fill the whole order (``FILLED``); else it ends ``CANCELED``."""
        return self.filled_qty >= self.order.quantity


def fill_matches(order: OrderView, fill: FillView) -> bool:
    """True when ``fill`` belongs to ``order``: same venue order id when the order has one, else same client id."""
    if fill.instrument_id != order.instrument_id:
        return False
    if order.venue_order_id is not None:
        return fill.venue_order_id == order.venue_order_id
    return order.client_order_id is not None and fill.client_order_id == order.client_order_id


def recover(orders: Iterable[OrderView], fills: Sequence[FillView], since_ns: int) -> tuple[Recovery, ...]:
    """The recovery of each order in ``orders`` that has fills since ``since_ns`` the cache does not hold yet."""
    recoveries: list[Recovery] = []
    for order in orders:
        new = sorted(
            (
                f
                for f in fills
                if f.ts_event >= since_ns and f.trade_id not in order.trade_ids and fill_matches(order, f)
            ),
            key=lambda f: (f.ts_event, f.trade_id),
        )
        unique = tuple({f.trade_id: f for f in new}.values())
        if unique:
            filled = order.filled_qty + sum((f.quantity for f in unique), Decimal(0))
            recoveries.append(Recovery(order=order, fills=unique, filled_qty=filled))
    return tuple(recoveries)


def window_complete(fills: Sequence[FillView], since_ns: int, page: int = KRAKEN_FILLS_PAGE) -> bool:
    """True when ``fills`` (one page of the exchange's newest fills) surely holds every fill since ``since_ns``."""
    if len(fills) < page:
        return True
    return min(f.ts_event for f in fills) < since_ns


def cache_untrusted_key(env: str) -> str:
    """The Redis set of instruments whose cached position is wrong (AC-8), e.g. ``live:reconcile:cache_untrusted``."""
    return f"{env}:reconcile:cache_untrusted"
