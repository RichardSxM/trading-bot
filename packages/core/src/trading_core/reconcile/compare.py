"""``compare``: the findings between an ``ExchangeView`` and a ``LocalView`` (spec 0023 AC-7, AC-8, AC-10, AC-11).

Orders match on ``venue_order_id``, falling back to ``client_order_id``; prices are never compared.
Positions match per instrument on side and on quantity, both rounded to the instrument's size
increment. An exchange order the trader does not hold is ``unknown_order``; a local order the
exchange does not have is ``local_only_order``. An exchange position with no local one is
``unknown_position``; a local position with no exchange one, or a different side or size, is
``position_mismatch``. Findings come out in a fixed order: orders before positions, each sorted.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from trading_core.reconcile.views import ExchangeView, LocalView, OrderView, PositionView
from trading_core.recording.enums import MismatchKind


@dataclass(frozen=True, kw_only=True)
class Finding:
    """One disagreement between the exchange and the trader, with each side's view of it."""

    kind: MismatchKind
    instrument_id: str
    client_order_id: str | None = None
    venue_order_id: str | None = None
    exchange_side: str | None = None
    exchange_qty: Decimal | None = None
    local_side: str | None = None
    local_qty: Decimal | None = None


@dataclass(frozen=True)
class Comparison:
    """The findings of one compare, in a fixed order; none means clean."""

    findings: tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        """True when the two views agree."""
        return not self.findings


def rounded(quantity: Decimal, increment: Decimal | None) -> Decimal:
    """``quantity`` rounded to the nearest multiple of ``increment`` (half even); unchanged without one."""
    if increment is None or increment <= 0:
        return quantity
    return (quantity / increment).to_integral_value() * increment


def _same_order(a: OrderView, b: OrderView) -> bool:
    if a.venue_order_id is not None and b.venue_order_id is not None:
        return a.venue_order_id == b.venue_order_id
    return a.client_order_id is not None and a.client_order_id == b.client_order_id


def _unmatched(orders: Iterable[OrderView], others: tuple[OrderView, ...]) -> list[OrderView]:
    return [order for order in orders if not any(_same_order(order, other) for other in others)]


def _order_key(order: OrderView) -> tuple[str, str, str]:
    return (order.instrument_id, order.venue_order_id or "", order.client_order_id or "")


def _order_finding(kind: MismatchKind, order: OrderView, *, on_exchange: bool) -> Finding:
    side, qty = str(order.side), order.quantity
    return Finding(
        kind=kind,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=order.venue_order_id,
        exchange_side=side if on_exchange else None,
        exchange_qty=qty if on_exchange else None,
        local_side=None if on_exchange else side,
        local_qty=None if on_exchange else qty,
    )


def _by_instrument(positions: Iterable[PositionView]) -> dict[str, PositionView]:
    return {position.instrument_id: position for position in positions}


def _position_finding(
    instrument_id: str, exchange: PositionView | None, local: PositionView | None, increment: Decimal | None
) -> Finding | None:
    if exchange is None and local is None:
        return None
    if exchange is not None and local is not None:
        same_side = exchange.side == local.side
        if same_side and rounded(exchange.quantity, increment) == rounded(local.quantity, increment):
            return None
    return Finding(
        kind=MismatchKind.UNKNOWN_POSITION if local is None else MismatchKind.POSITION_MISMATCH,
        instrument_id=instrument_id,
        exchange_side=None if exchange is None else str(exchange.side),
        exchange_qty=None if exchange is None else exchange.quantity,
        local_side=None if local is None else str(local.side),
        local_qty=None if local is None else local.quantity,
    )


def compare(exchange: ExchangeView, local: LocalView) -> Comparison:
    """The findings between what the exchange reported and what the trader believes."""
    findings = [
        _order_finding(MismatchKind.UNKNOWN_ORDER, order, on_exchange=True)
        for order in sorted(_unmatched(exchange.orders, local.orders), key=_order_key)
    ]
    findings += [
        _order_finding(MismatchKind.LOCAL_ONLY_ORDER, order, on_exchange=False)
        for order in sorted(_unmatched(local.orders, exchange.orders), key=_order_key)
    ]
    on_exchange, held = _by_instrument(exchange.positions), _by_instrument(local.positions)
    for instrument_id in sorted(on_exchange.keys() | held.keys()):
        finding = _position_finding(
            instrument_id,
            on_exchange.get(instrument_id),
            held.get(instrument_id),
            local.size_increments.get(instrument_id),
        )
        if finding is not None:
            findings.append(finding)
    return Comparison(tuple(findings))
