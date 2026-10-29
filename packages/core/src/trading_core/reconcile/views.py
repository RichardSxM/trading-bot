"""The two sides a reconciliation check compares (spec 0023 *Where the views come from*).

Sizes and prices are ``Decimal``, times UTC nanoseconds. An ``ExchangePort`` either returns a
whole ``ExchangeView`` or raises ``ExchangeUnavailable``: an error never reads as "no orders, no
position" (AC-4).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

from trading_core.recording.enums import OrderSide, PositionSide


class ExchangeUnavailable(Exception):
    """The exchange's view could not be fetched (an error, a timeout, or an HTTP failure)."""


@dataclass(frozen=True, kw_only=True)
class OrderView:
    """One open order: matched on ``venue_order_id``, falling back to ``client_order_id`` (prices are not compared).

    ``strategy_id`` and ``ts_init`` are known only for a local order; the exchange reports neither.
    """

    instrument_id: str
    client_order_id: str | None
    venue_order_id: str | None
    side: OrderSide
    order_type: str
    quantity: Decimal
    trigger_price: Decimal | None = None
    reduce_only: bool = False
    strategy_id: str | None = None
    ts_init: int = 0

    @property
    def is_stop(self) -> bool:
        """True for a reduce only stop (the protective stop of a position)."""
        return self.reduce_only and self.order_type in STOP_TYPES


# The order types a protective stop has (``snake`` of Nautilus's ``OrderType`` names).
STOP_TYPES: Final = frozenset({"stop_market", "stop_limit"})


@dataclass(frozen=True, kw_only=True)
class PositionView:
    """One open position on an instrument (netting: at most one per instrument on the exchange)."""

    instrument_id: str
    side: PositionSide
    quantity: Decimal


@dataclass(frozen=True, kw_only=True)
class FillOrder:
    """An order that filled into a held position: whose it is, and whether it was an entry."""

    client_order_id: str
    strategy_id: str
    reduce_only: bool
    generated: bool = False  # Nautilus made it during reconciliation (strategy ``EXTERNAL``), never ours


@dataclass(frozen=True, kw_only=True)
class HeldPosition(PositionView):
    """A position open in the cache, with what AC-6 and AC-12 need to judge it.

    ``fill_orders`` are the orders whose fills make up the position since it was last flat, each
    once. ``opening_order_id`` is the first of them, the parent the stop is tagged with.
    """

    position_id: str = ""
    strategy_id: str = ""
    opening_order_id: str | None = None
    ts_opened: int = 0
    fill_orders: tuple[FillOrder, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ExchangeView:
    """What the exchange reported: its open orders and positions, fetched at ``fetched_ns``."""

    orders: tuple[OrderView, ...]
    positions: tuple[PositionView, ...]
    fetched_ns: int


@dataclass(frozen=True, kw_only=True)
class LocalView:
    """What the trader believes: the cache's open orders of its own and its open positions.

    ``size_increments`` maps an instrument to its size increment; quantities are compared after
    rounding both to it. An instrument without one is compared exactly. ``owned_strategies`` are
    the strategy ids whose orders are ours (the loaded strategies and the ``ReconcileExecutor``).
    ``stop_triggers`` maps an instrument to the recorded trigger price of its position's stop
    (spec 0023 *Value sourcing*); an instrument without one has none.
    """

    orders: tuple[OrderView, ...]
    positions: tuple[HeldPosition, ...]
    size_increments: Mapping[str, Decimal] = field(default_factory=dict)
    owned_strategies: frozenset[str] = frozenset()
    stop_triggers: Mapping[str, Decimal] = field(default_factory=dict)


def exchange_snapshot_data(view: ExchangeView | None, fetched_ns: int) -> dict[str, object]:
    """The ``{env}:snapshot:exchange`` data: ``ok`` with what the exchange reported, or ``unknown`` when ``view``
    is None (the fetch failed), with no orders or positions."""
    if view is None:
        return {"status": "unknown", "fetched_ns": fetched_ns}
    return {
        "status": "ok",
        "fetched_ns": view.fetched_ns,
        "positions": [
            {"instrument_id": p.instrument_id, "side": str(p.side), "quantity": str(p.quantity)} for p in view.positions
        ],
        "orders": [
            {
                "client_order_id": o.client_order_id,
                "venue_order_id": o.venue_order_id,
                "instrument_id": o.instrument_id,
                "side": str(o.side),
                "order_type": o.order_type,
                "quantity": str(o.quantity),
                "trigger_price": None if o.trigger_price is None else str(o.trigger_price),
                "reduce_only": o.reduce_only,
            }
            for o in view.orders
        ],
    }


class ExchangePort(Protocol):
    """Fetches the exchange's view; raises ``ExchangeUnavailable`` on any failure."""

    async def fetch(self) -> ExchangeView:
        """The exchange's open orders and positions, or ``ExchangeUnavailable``."""
        ...


class CancelPort(Protocol):
    """Cancels an order on the exchange directly, also one the trader's cache does not hold."""

    async def cancel(self, order: OrderView) -> None:
        """Cancel ``order``; raises ``ExchangeUnavailable`` when the exchange did not confirm it."""
        ...
