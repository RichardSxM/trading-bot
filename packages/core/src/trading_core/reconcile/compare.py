"""``compare``: the findings between an ``ExchangeView`` and a ``LocalView`` (spec 0023 AC-6 to AC-12).

Orders match on ``venue_order_id``, falling back to ``client_order_id``; prices are never compared.
An exchange order the trader does not hold as its own is ``unknown_order``; a local order the
exchange does not have is ``local_only_order``. Two open entry orders of one strategy on one
instrument, or a position filled by two entry orders of one strategy, is ``duplicate_entry``.

Positions match per instrument on side and on quantity, both rounded to the instrument's size
increment. A held position is *known* when it is the only one on its instrument, it agrees with
the exchange's, and every order that filled into it is ours (AC-6). An exchange position with no
held one is ``unknown_position``; anything else that is not known is ``position_mismatch``.

Every known position needs exactly one reduce only stop of ours on the exchange, closing the full
size at the recorded trigger price (AC-9): none is ``missing_stop``, anything else ``stop_size_mismatch``.

Findings come out in a fixed order: unknown orders, local only orders, duplicates, positions,
stops, each sorted.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from trading_core.reconcile.views import ExchangeView, HeldPosition, LocalView, OrderView, PositionView
from trading_core.recording.enums import MismatchKind, OrderSide, PositionSide

# Why a finding was raised, in its ``details`` (one per case the kind alone does not tell).
REASON_NO_HELD_POSITION = "no_held_position"
REASON_NOT_ON_EXCHANGE = "not_on_exchange"
REASON_SIDE_OR_SIZE = "side_or_size"
REASON_SPLIT = "split_ownership"
REASON_NOT_OURS = "fills_not_ours"
REASON_OPEN_ENTRIES = "open_entries"
REASON_FILLED_ENTRIES = "filled_entries"
REASON_NO_STOP = "no_stop"
REASON_STOP_WRONG = "stop_wrong"


@dataclass(frozen=True, kw_only=True)
class Finding:
    """One disagreement between the exchange and the trader, with each side's view of it.

    ``orders`` are the orders it is about beyond the one named (a duplicate's entries, or the
    exchange's stops on a position); ``position`` is the held position it is about, if any.
    """

    kind: MismatchKind
    instrument_id: str
    client_order_id: str | None = None
    venue_order_id: str | None = None
    exchange_side: str | None = None
    exchange_qty: Decimal | None = None
    local_side: str | None = None
    local_qty: Decimal | None = None
    reason: str | None = None
    orders: tuple[OrderView, ...] = ()
    position: HeldPosition | None = None
    exchange_position: PositionView | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        """What a second fetch must also find for this finding to be confirmed (AC-27)."""
        return (str(self.kind), self.instrument_id, self.venue_order_id or "", self.client_order_id or "")


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


def closing_side(side: PositionSide) -> OrderSide:
    """The order side that closes a position of ``side``."""
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def is_ours(position: HeldPosition, owned: frozenset[str]) -> bool:
    """True when the position and every order that filled into it are ours, none generated (AC-6)."""
    if position.strategy_id not in owned or not position.fill_orders:
        return False
    return all(o.strategy_id in owned and not o.generated for o in position.fill_orders)


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
        orders=(order,),
    )


def _duplicates(local: LocalView) -> list[Finding]:
    """AC-12: two open entry orders of one strategy on one instrument, or a position two entries filled."""
    findings: list[Finding] = []
    open_entries: dict[tuple[str, str], list[OrderView]] = defaultdict(list)
    for order in local.orders:
        if not order.reduce_only:
            open_entries[(order.strategy_id or "", order.instrument_id)].append(order)
    for (_, instrument_id), entries in sorted(open_entries.items()):
        if len(entries) >= 2:
            findings.append(
                Finding(
                    kind=MismatchKind.DUPLICATE_ENTRY,
                    instrument_id=instrument_id,
                    reason=REASON_OPEN_ENTRIES,
                    orders=tuple(sorted(entries, key=_order_key)),
                )
            )
    for position in sorted(local.positions, key=lambda p: (p.instrument_id, p.position_id)):
        filled: dict[str, set[str]] = defaultdict(set)
        for fill_order in position.fill_orders:
            if not fill_order.reduce_only:
                filled[fill_order.strategy_id].add(fill_order.client_order_id)
        if any(len(ids) >= 2 for ids in filled.values()):
            findings.append(
                Finding(
                    kind=MismatchKind.DUPLICATE_ENTRY,
                    instrument_id=position.instrument_id,
                    local_side=str(position.side),
                    local_qty=position.quantity,
                    reason=REASON_FILLED_ENTRIES,
                    position=position,
                )
            )
    return findings


def _signed(side: PositionSide, quantity: Decimal) -> Decimal:
    return quantity if side is PositionSide.LONG else -quantity


def _net(positions: list[HeldPosition]) -> tuple[str | None, Decimal | None]:
    """The side and size the held positions on one instrument add up to (None, None when there are none)."""
    if not positions:
        return None, None
    total = sum((_signed(p.side, p.quantity) for p in positions), Decimal(0))
    if total == 0:
        return str(PositionSide.FLAT), Decimal(0)
    return str(PositionSide.LONG if total > 0 else PositionSide.SHORT), abs(total)


def _position_finding(
    instrument_id: str,
    exchange: PositionView | None,
    held: list[HeldPosition],
    local: LocalView,
) -> Finding | None:
    increment = local.size_increments.get(instrument_id)
    if exchange is None and not held:
        return None
    reason: str
    if not held:
        reason = REASON_NO_HELD_POSITION
    elif exchange is None:
        reason = REASON_NOT_ON_EXCHANGE
    elif len(held) > 1:
        reason = REASON_SPLIT
    elif held[0].side != exchange.side or rounded(held[0].quantity, increment) != rounded(exchange.quantity, increment):
        reason = REASON_SIDE_OR_SIZE
    elif not is_ours(held[0], local.owned_strategies):
        reason = REASON_NOT_OURS
    else:
        return None
    local_side, local_qty = _net(held)
    return Finding(
        kind=MismatchKind.UNKNOWN_POSITION if not held else MismatchKind.POSITION_MISMATCH,
        instrument_id=instrument_id,
        exchange_side=None if exchange is None else str(exchange.side),
        exchange_qty=None if exchange is None else exchange.quantity,
        local_side=local_side,
        local_qty=local_qty,
        reason=reason,
        position=held[0] if len(held) == 1 else None,
        exchange_position=exchange,
    )


def _stop_finding(position: HeldPosition, exchange: ExchangeView, local: LocalView) -> Finding | None:
    """AC-9: exactly one reduce only stop closing the full size at the recorded trigger price."""
    instrument_id = position.instrument_id
    increment = local.size_increments.get(instrument_id)
    # Only our own stops count: an unknown one is cancelled as ``unknown_order`` first.
    stops = tuple(
        sorted(
            (
                o
                for o in exchange.orders
                if o.instrument_id == instrument_id and o.is_stop and any(_same_order(o, m) for m in local.orders)
            ),
            key=_order_key,
        )
    )
    if len(stops) == 1:
        (stop,) = stops
        target = local.stop_triggers.get(instrument_id)
        if (
            stop.side is closing_side(position.side)
            and rounded(stop.quantity, increment) == rounded(position.quantity, increment)
            and target is not None
            and stop.trigger_price == target
        ):
            return None
    return Finding(
        kind=MismatchKind.MISSING_STOP if not stops else MismatchKind.STOP_SIZE_MISMATCH,
        instrument_id=instrument_id,
        exchange_side=str(closing_side(position.side)) if stops else None,
        exchange_qty=sum((s.quantity for s in stops), Decimal(0)) if stops else None,
        local_side=str(position.side),
        local_qty=position.quantity,
        reason=REASON_NO_STOP if not stops else REASON_STOP_WRONG,
        orders=stops,
        position=position,
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
    duplicates = _duplicates(local)
    findings += duplicates
    duplicated = {f.instrument_id for f in duplicates}
    on_exchange = {p.instrument_id: p for p in exchange.positions}
    held: dict[str, list[HeldPosition]] = defaultdict(list)
    for position in local.positions:
        held[position.instrument_id].append(position)
    known: list[HeldPosition] = []
    for instrument_id in sorted(on_exchange.keys() | held.keys()):
        finding = _position_finding(instrument_id, on_exchange.get(instrument_id), held[instrument_id], local)
        if finding is not None:
            findings.append(finding)
        elif instrument_id not in duplicated:
            known.append(held[instrument_id][0])
    for position in known:
        stop_finding = _stop_finding(position, exchange, local)
        if stop_finding is not None:
            findings.append(stop_finding)
    return Comparison(tuple(findings))


def known_positions(exchange: ExchangeView, local: LocalView) -> tuple[HeldPosition, ...]:
    """The held positions AC-6 adopts: the only one on its instrument, agreeing with the exchange, all ours."""
    on_exchange = {p.instrument_id: p for p in exchange.positions}
    held: dict[str, list[HeldPosition]] = defaultdict(list)
    for position in local.positions:
        held[position.instrument_id].append(position)
    return tuple(
        positions[0]
        for instrument_id, positions in sorted(held.items())
        if _position_finding(instrument_id, on_exchange.get(instrument_id), positions, local) is None
    )
