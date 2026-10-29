"""The two sides a reconciliation check compares (spec 0023 *Where the views come from*).

Sizes and prices are ``Decimal``, times UTC nanoseconds. An ``ExchangePort`` either returns a
whole ``ExchangeView`` or raises ``ExchangeUnavailable``: an error never reads as "no orders, no
position" (AC-4).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from trading_core.recording.enums import OrderSide, PositionSide


class ExchangeUnavailable(Exception):
    """The exchange's view could not be fetched (an error, a timeout, or an HTTP failure)."""


@dataclass(frozen=True, kw_only=True)
class OrderView:
    """One open order: matched on ``venue_order_id``, falling back to ``client_order_id`` (prices are not compared)."""

    instrument_id: str
    client_order_id: str | None
    venue_order_id: str | None
    side: OrderSide
    order_type: str
    quantity: Decimal
    trigger_price: Decimal | None = None
    reduce_only: bool = False


@dataclass(frozen=True, kw_only=True)
class PositionView:
    """One open position on an instrument (netting: at most one per instrument)."""

    instrument_id: str
    side: PositionSide
    quantity: Decimal


@dataclass(frozen=True, kw_only=True)
class ExchangeView:
    """What the exchange reported: its open orders and positions, fetched at ``fetched_ns``."""

    orders: tuple[OrderView, ...]
    positions: tuple[PositionView, ...]
    fetched_ns: int


@dataclass(frozen=True, kw_only=True)
class LocalView:
    """What the trader believes: the cache's open orders and positions.

    ``size_increments`` maps an instrument to its size increment; quantities are compared after
    rounding both to it. An instrument without one is compared exactly.
    """

    orders: tuple[OrderView, ...]
    positions: tuple[PositionView, ...]
    size_increments: Mapping[str, Decimal] = field(default_factory=dict)


class ExchangePort(Protocol):
    """Fetches the exchange's view; raises ``ExchangeUnavailable`` on any failure."""

    async def fetch(self) -> ExchangeView:
        """The exchange's open orders and positions, or ``ExchangeUnavailable``."""
        ...
