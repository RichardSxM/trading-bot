"""Plain Nautilus facts (ids, ``Decimal``s, ns ints, enum names, tags) to recording rows (spec 0006 AC-11).

No Nautilus import: the recording actor reads the cache and events and passes plain values,
so these rules are testable on their own. Nautilus enum names (``STOP_MARKET``) become the
stored snake case values (``stop_market``); event class names (``OrderFilled``) become event
kinds (``order_filled``). Averages are computed here from fills as ``Decimal``, never taken
from Nautilus's float ``avg_px``.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from trading_core.recording.enums import (
    Env,
    Liquidity,
    OrderEventKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    TimeInForce,
)
from trading_core.recording.rows import (
    FillRow,
    JsonObject,
    OrderEventRow,
    OrderRow,
    PositionRow,
    ns_to_utc,
)

DECISION_TAG: Final = "decision:"
PARENT_TAG: Final = "parent:"
ORIGIN_TAG: Final = "origin:"
HALT_TAG: Final = "halt:"
_CAMEL: Final = re.compile(r"(?<!^)(?=[A-Z])")


class MissingDecisionTagError(ValueError):
    """A strategy order carries no ``decision:<uuid>`` tag: a bug, so recording fails closed."""


def snake(name: str) -> str:
    """``STOP_MARKET`` to ``stop_market`` and ``OrderFilled`` to ``order_filled``."""
    return name.lower() if name.isupper() or "_" in name else _CAMEL.sub("_", name).lower()


def liquidity(name: str) -> Liquidity:
    """Nautilus ``LiquiditySide`` name to ``Liquidity`` (``NO_LIQUIDITY_SIDE`` is ``no_liquidity``)."""
    return Liquidity.NO_LIQUIDITY if name == "NO_LIQUIDITY_SIDE" else Liquidity(snake(name))


def tag_value(tags: Iterable[str], prefix: str) -> str | None:
    """The value of the first tag starting with ``prefix``, else None."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag.removeprefix(prefix)
    return None


def decision_id(tags: Iterable[str]) -> UUID:
    """The decision a strategy order carries in its ``decision:<uuid>`` tag; raises when missing or malformed."""
    value = tag_value(tags, DECISION_TAG)
    if value is None:
        raise MissingDecisionTagError(f"strategy order has no decision tag: {list(tags)}")
    try:
        return UUID(value)
    except ValueError as exc:
        raise MissingDecisionTagError(f"bad decision tag {value!r}") from exc


def order_origin(tags: Iterable[str]) -> OrderOrigin:
    """The origin an order carries in its ``origin:<value>`` tag; ``strategy`` without one.

    Only ``kill_switch`` may be tagged (spec 0010 AC-1); any other value raises
    ``MissingDecisionTagError``, so a mistagged order fails closed like an untagged one.
    """
    value = tag_value(tags, ORIGIN_TAG)
    if value is None:
        return OrderOrigin.STRATEGY
    if value != OrderOrigin.KILL_SWITCH:
        raise MissingDecisionTagError(f"bad origin tag {value!r}")
    return OrderOrigin.KILL_SWITCH


def weighted_avg(fills: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """The quantity weighted average price of ``(price, quantity)`` fills; None without any quantity."""
    quantity = sum((q for _, q in fills), Decimal(0))
    if quantity == 0:
        return None
    return sum((p * q for p, q in fills), Decimal(0)) / quantity


@dataclass(frozen=True, kw_only=True)
class OrderFacts:
    """An order as the cache holds it after an event, in plain values (enum fields are Nautilus names)."""

    client_order_id: str
    venue_order_id: str | None
    instrument_id: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    price: Decimal | None
    trigger_price: Decimal | None
    reduce_only: bool
    post_only: bool
    status: str
    filled_qty: Decimal
    avg_px: Decimal | None
    tags: Sequence[str]


def order_row(facts: OrderFacts, *, env: Env, run_id: UUID, last_event_ns: int) -> OrderRow:
    """The ``orders`` row for a strategy or kill switch order; its origin, decision, and parent come from its tags.

    A ``strategy`` order must carry a decision; a ``kill_switch`` order has none (spec 0010 AC-1).
    """
    origin = order_origin(facts.tags)
    return OrderRow(
        env=env,
        run_id=run_id,
        decision_id=decision_id(facts.tags) if origin is OrderOrigin.STRATEGY else None,
        origin=origin,
        client_order_id=facts.client_order_id,
        venue_order_id=facts.venue_order_id,
        parent_client_order_id=tag_value(facts.tags, PARENT_TAG),
        instrument_id=facts.instrument_id,
        side=OrderSide(snake(facts.side)),
        order_type=OrderType(snake(facts.order_type)),
        time_in_force=TimeInForce(snake(facts.time_in_force)),
        quantity=facts.quantity,
        price=facts.price,
        trigger_price=facts.trigger_price,
        reduce_only=facts.reduce_only,
        post_only=facts.post_only,
        status=OrderStatus(snake(facts.status)),
        filled_qty=facts.filled_qty,
        avg_px=facts.avg_px,
        last_event_ns=last_event_ns,
    )


def order_event_row(
    *,
    env: Env,
    run_id: UUID,
    client_order_id: str,
    event_id: UUID,
    event_class: str,
    details: JsonObject,
    ts_event: int,
) -> OrderEventRow:
    """The ``order_events`` row for one Nautilus order event (``event_class`` is its class name)."""
    return OrderEventRow(
        env=env,
        run_id=run_id,
        client_order_id=client_order_id,
        event_id=event_id,
        kind=OrderEventKind(snake(event_class)),
        details=details,
        occurred_ns=ts_event,
    )


@dataclass(frozen=True, kw_only=True)
class FillFacts:
    """One ``OrderFilled`` in plain values."""

    client_order_id: str
    instrument_id: str
    trade_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    liquidity_side: str
    ts_event: int


def fill_row(facts: FillFacts, *, env: Env, run_id: UUID, position_id: UUID | None) -> FillRow:
    """The ``fills`` row; ``position_id`` is None at first and set once the position event arrives."""
    return FillRow(
        env=env,
        run_id=run_id,
        client_order_id=facts.client_order_id,
        position_id=position_id,
        instrument_id=facts.instrument_id,
        trade_id=facts.trade_id,
        side=OrderSide(snake(facts.side)),
        quantity=facts.quantity,
        price=facts.price,
        fee=facts.fee,
        fee_currency=facts.fee_currency,
        liquidity=liquidity(facts.liquidity_side),
        filled_ns=facts.ts_event,
    )


@dataclass(frozen=True, kw_only=True)
class PositionFacts:
    """A position after a position event, in plain values.

    ``opening`` and ``closing`` are its ``(price, quantity)`` fills in and against the entry side.
    """

    position_ref: str
    instrument_id: str
    side: str
    quantity: Decimal
    peak_qty: Decimal
    opening: Sequence[tuple[Decimal, Decimal]]
    closing: Sequence[tuple[Decimal, Decimal]]
    realized_pnl: Decimal
    fees_total: Decimal
    currency: str
    opened_ns: int
    closed_ns: int | None
    last_event_ns: int


def position_row(facts: PositionFacts, *, env: Env, run_id: UUID, row_id: UUID) -> PositionRow:
    """The ``positions`` row under the id the actor keeps for ``(position_ref, opened_ns)``.

    ``funding_total`` is written here only on insert (as 0); ``record_funding`` keeps it after that
    (spec 0011 AC-14), and ``realized_pnl`` stays NautilusTrader's, before funding.
    """
    avg_entry = weighted_avg(facts.opening)
    if avg_entry is None:
        raise ValueError(f"position {facts.position_ref} has no opening fill")
    closed = facts.closed_ns is not None
    return PositionRow(
        id=row_id,
        env=env,
        run_id=run_id,
        instrument_id=facts.instrument_id,
        position_ref=facts.position_ref,
        side=PositionSide(snake(facts.side)),
        status=PositionStatus.CLOSED if closed else PositionStatus.OPEN,
        quantity=facts.quantity,
        peak_qty=facts.peak_qty,
        avg_entry_px=avg_entry,
        avg_exit_px=weighted_avg(facts.closing),
        realized_pnl=facts.realized_pnl,
        fees_total=facts.fees_total,
        funding_total=Decimal(0),
        currency=facts.currency,
        opened_ns=facts.opened_ns,
        closed_at=ns_to_utc(facts.closed_ns) if facts.closed_ns is not None else None,
        last_event_ns=facts.last_event_ns,
    )
