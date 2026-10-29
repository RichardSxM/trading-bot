"""``plan``: what the trader does about each finding of a check (spec 0023 AC-7 to AC-12, AC-23, AC-28).

Pure: it takes the comparison, the local view, the check's ``Mode``, the last fresh quote mid per
instrument, and the fill recoveries of local only orders, and returns one ``Planned`` per finding
with its action, its steps, and whether it halts. The actor runs the steps in a fixed phase order
(``Phase``): cancel unknown orders, recover missed fills, mark the other local only orders, align
the cache with the exchange, fix stops, then halt and flatten.

- ``unknown_order``: cancel it on the exchange.
- ``local_only_order``: with a recovery, feed its fills to Nautilus (``adopted``, ``cache_fix:
  recovered``, AC-11); else mark it canceled locally through Nautilus reconciliation.
- ``duplicate_entry``: cancel every open entry on the instrument, flatten, halt.
- ``unknown_position`` / ``position_mismatch``: halt and flatten. When the exchange holds more than
  the cache (or the cache holds nothing), the exchange's position is first fed to Nautilus, which
  adds the difference as an ``EXTERNAL`` position the ``ReconcileExecutor`` can close (``cache_fix:
  synced``). When the cache holds more than the exchange, or the other side, Nautilus 1.231.0
  cannot shrink a strategy's position that way (AC-8, ``cache_fix: manual``): the instrument goes
  into the untrusted set, the ``ReconcileExecutor`` closes what the exchange holds from a fresh
  fetch, and the cache is left for a hand reset.
- ``missing_stop`` / ``stop_size_mismatch`` (AC-9): with no recorded trigger price, or our open
  stops on different prices, flatten and halt. When the target stop is already there, only cancel
  the rest. Else, with the mid already through the trigger, close at market (no halt); without a
  fresh quote, wait for one (``awaiting_quote``), then flatten and halt; else place the target stop
  and, once it is accepted, cancel the rest. A halting finding skips every stop fix: the flatten
  closes those positions too.

In ``Mode.REPORT`` (flattening or halted, AC-28) only unknown orders that are not reduce only are
cancelled, local only orders are still recovered or marked (that only corrects the books), a
``cache_fix: manual`` finding still marks the instrument untrusted and closes the exchange's
position, and nothing halts again; in ``Mode.DEMO`` (AC-23) nothing is acted on.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum, StrEnum

from trading_core.reconcile.compare import Comparison, Finding, closing_side, rounded
from trading_core.reconcile.recover import Recovery
from trading_core.reconcile.views import LocalView, OrderView, PositionView
from trading_core.recording.enums import MismatchAction, MismatchKind, OrderSide, PositionSide

# Why a finding halts or was left alone, in its ``details``.
NO_TRIGGER_PRICE = "no_trigger_price"
STOP_PRICES_DISAGREE = "stop_prices_disagree"
NO_FRESH_QUOTE = "no_fresh_quote"
HALTED_ELSEWHERE = "halting_finding"
# How the cache was brought in line with the exchange, in a finding's ``details["cache_fix"]``.
CACHE_SYNCED = "synced"
CACHE_RECOVERED = "recovered"
CACHE_MANUAL = "manual"


class Mode(StrEnum):
    """How far a check may act: fully, only on unknown entries (flattening or halted), or not at all (demo)."""

    ACT = "act"
    REPORT = "report"
    DEMO = "demo"


class Phase(IntEnum):
    """The fixed order steps run in within one check."""

    CANCEL = 1
    RECOVER = 2
    MARK = 3
    SYNC = 4
    STOP = 5


@dataclass(frozen=True, kw_only=True)
class CancelOrder:
    """Cancel an order on the exchange."""

    order: OrderView
    phase: Phase = Phase.CANCEL


@dataclass(frozen=True, kw_only=True)
class RecoverFills:
    """Feed a local only order's missed fills to Nautilus, then its terminal status (AC-11)."""

    recovery: Recovery
    phase: Phase = Phase.RECOVER


@dataclass(frozen=True, kw_only=True)
class MarkUntrusted:
    """Add an instrument to the untrusted cache set: its cached position is wrong (AC-8)."""

    instrument_id: str
    phase: Phase = Phase.SYNC


@dataclass(frozen=True, kw_only=True)
class CloseResidual:
    """Close what the exchange holds on an untrusted instrument, from a fresh fetch, reduce only (AC-8)."""

    instrument_id: str
    phase: Phase = Phase.SYNC


@dataclass(frozen=True, kw_only=True)
class MarkCanceled:
    """Mark a local order canceled through Nautilus reconciliation (the exchange does not have it)."""

    order: OrderView
    phase: Phase = Phase.MARK


@dataclass(frozen=True, kw_only=True)
class SyncPosition:
    """Feed the exchange's position on an instrument (None: flat) to Nautilus, so the cache holds it."""

    instrument_id: str
    exchange: PositionView | None
    phase: Phase = Phase.SYNC


@dataclass(frozen=True, kw_only=True)
class PlaceStop:
    """Place the target stop for a held position; once accepted, cancel ``then_cancel`` (never naked)."""

    instrument_id: str
    position_id: str
    side: OrderSide
    quantity: Decimal
    trigger_price: Decimal
    parent_client_order_id: str | None
    then_cancel: tuple[OrderView, ...]
    phase: Phase = Phase.STOP


@dataclass(frozen=True, kw_only=True)
class CloseAtMarket:
    """Close a held position with a reduce only market order (its stop's trigger is already crossed)."""

    instrument_id: str
    position_id: str
    phase: Phase = Phase.STOP


type Step = (
    CancelOrder | RecoverFills | MarkCanceled | SyncPosition | MarkUntrusted | CloseResidual | PlaceStop | CloseAtMarket
)


@dataclass(frozen=True, kw_only=True)
class Planned:
    """One finding with what is done about it."""

    finding: Finding
    action: MismatchAction
    steps: tuple[Step, ...] = ()
    halts: bool = False
    flattens: bool = False
    awaiting_quote: bool = False
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """Every finding's plan, in the comparison's order."""

    planned: tuple[Planned, ...]

    @property
    def halts(self) -> bool:
        """True when a finding latches a halt."""
        return any(p.halts for p in self.planned)

    @property
    def flattens(self) -> bool:
        """True when the halt should also flatten."""
        return any(p.flattens for p in self.planned)

    @property
    def awaiting_quote(self) -> frozenset[str]:
        """The instruments whose stop fix waits for a fresh quote."""
        return frozenset(p.finding.instrument_id for p in self.planned if p.awaiting_quote)

    @property
    def recovers(self) -> bool:
        """True when a step feeds missed fills to Nautilus: the check compares again before the rest (AC-11)."""
        return any(isinstance(s, RecoverFills) for p in self.planned for s in p.steps)

    def steps(self) -> list[tuple[Planned, Step]]:
        """Every step with its finding, in phase order (stable within a phase)."""
        pairs = [(p, s) for p in self.planned for s in p.steps]
        return sorted(pairs, key=lambda pair: pair[1].phase)


def _signed(side: PositionSide | str | None, quantity: Decimal | None) -> Decimal:
    if quantity is None or side is None:
        return Decimal(0)
    return quantity if str(side) == PositionSide.LONG else -quantity if str(side) == PositionSide.SHORT else Decimal(0)


def _sizes(finding: Finding, local: LocalView) -> tuple[Decimal, Decimal]:
    """The exchange's and the cache's signed size on the finding's instrument, both rounded."""
    increment = local.size_increments.get(finding.instrument_id)
    on_exchange = rounded(_signed(finding.exchange_side, finding.exchange_qty), increment)
    held = rounded(_signed(finding.local_side, finding.local_qty), increment)
    return on_exchange, held


def _cache_holds_more(finding: Finding, local: LocalView) -> bool:
    """True when the cache holds more than the exchange, or the other side (AC-8)."""
    if finding.kind is not MismatchKind.POSITION_MISMATCH:
        return False
    on_exchange, held = _sizes(finding, local)
    return held != 0 and on_exchange != held and not (held * on_exchange > 0 and abs(on_exchange) > abs(held))


def _manual(finding: Finding, *, halts: bool) -> Planned:
    """AC-8: mark the instrument untrusted and close what the exchange holds; the cache waits for a hand reset."""
    steps = (MarkUntrusted(instrument_id=finding.instrument_id), CloseResidual(instrument_id=finding.instrument_id))
    exchange_flat = not finding.exchange_qty
    return Planned(
        finding=finding,
        action=MismatchAction.NONE if exchange_flat else MismatchAction.FLATTENED,
        steps=steps,
        halts=halts,
        flattens=halts,
        details={"cache_fix": CACHE_MANUAL},
    )


def _position(finding: Finding, local: LocalView) -> Planned:
    on_exchange, held = _sizes(finding, local)
    if on_exchange == held:
        # Not ours, or split between owners: the cache already adds up, so the flatten closes it.
        return Planned(finding=finding, action=MismatchAction.FLATTENED, halts=True, flattens=True)
    if _cache_holds_more(finding, local):
        return _manual(finding, halts=True)
    step = SyncPosition(instrument_id=finding.instrument_id, exchange=finding.exchange_position)
    return Planned(
        finding=finding,
        action=MismatchAction.FLATTENED,
        steps=(step,),
        halts=True,
        flattens=True,
        details={"cache_fix": CACHE_SYNCED},
    )


def _local_only(finding: Finding, recoveries: Mapping[str, Recovery]) -> Planned:
    """AC-11: feed the order's missed fills when there are any, else mark it canceled."""
    order = _order(finding)
    recovery = None if order.client_order_id is None else recoveries.get(order.client_order_id)
    if recovery is None:
        return Planned(finding=finding, action=MismatchAction.MARKED_CANCELED, steps=(MarkCanceled(order=order),))
    return Planned(
        finding=finding,
        action=MismatchAction.ADOPTED,
        steps=(RecoverFills(recovery=recovery),),
        details={
            "cache_fix": CACHE_RECOVERED,
            "fills": str(len(recovery.fills)),
            "filled_qty": str(recovery.filled_qty),
            "order_status": "filled" if recovery.complete else "canceled",
        },
    )


def _halt_and_flatten(finding: Finding, why: str) -> Planned:
    return Planned(finding=finding, action=MismatchAction.FLATTENED, halts=True, flattens=True, details={"why": why})


def _stop(finding: Finding, local: LocalView, mids: Mapping[str, Decimal], quote_waited: bool) -> Planned:
    position = finding.position
    assert position is not None  # a stop finding is always about a held position
    target = local.stop_triggers.get(finding.instrument_id)
    if target is None:
        return _halt_and_flatten(finding, NO_TRIGGER_PRICE)
    stops = finding.orders
    if len({s.trigger_price for s in stops}) > 1:
        return _halt_and_flatten(finding, STOP_PRICES_DISAGREE)
    increment = local.size_increments.get(finding.instrument_id)
    side = closing_side(position.side)
    full = rounded(position.quantity, increment)
    keep = next(
        (s for s in stops if s.trigger_price == target and s.side is side and rounded(s.quantity, increment) == full),
        None,
    )
    if keep is not None:
        cancels = tuple(CancelOrder(order=s) for s in stops if s is not keep)
        return Planned(finding=finding, action=MismatchAction.STOP_REPLACED, steps=cancels)
    mid = mids.get(finding.instrument_id)
    if mid is None:
        if quote_waited:
            return _halt_and_flatten(finding, NO_FRESH_QUOTE)
        return Planned(finding=finding, action=MismatchAction.NONE, awaiting_quote=True)
    crossed = mid <= target if position.side is PositionSide.LONG else mid >= target
    if crossed:
        close = CloseAtMarket(instrument_id=finding.instrument_id, position_id=position.position_id)
        return Planned(
            finding=finding,
            action=MismatchAction.CLOSED_AT_MARKET,
            steps=(close,),
            details={"mid": str(mid), "trigger_price": str(target)},
        )
    place = PlaceStop(
        instrument_id=finding.instrument_id,
        position_id=position.position_id,
        side=side,
        quantity=position.quantity,
        trigger_price=target,
        parent_client_order_id=position.opening_order_id,
        then_cancel=stops,
    )
    return Planned(
        finding=finding, action=MismatchAction.STOP_REPLACED, steps=(place,), details={"trigger_price": str(target)}
    )


def _act(
    finding: Finding,
    local: LocalView,
    mids: Mapping[str, Decimal],
    quote_waited: bool,
    recoveries: Mapping[str, Recovery],
) -> Planned:
    match finding.kind:
        case MismatchKind.UNKNOWN_ORDER:
            return Planned(
                finding=finding, action=MismatchAction.CANCELLED, steps=(CancelOrder(order=_order(finding)),)
            )
        case MismatchKind.LOCAL_ONLY_ORDER:
            return _local_only(finding, recoveries)
        case MismatchKind.DUPLICATE_ENTRY:
            entries = [o for o in local.orders if o.instrument_id == finding.instrument_id and not o.reduce_only]
            cancels = tuple(CancelOrder(order=o) for o in entries)
            return Planned(finding=finding, action=MismatchAction.FLATTENED, steps=cancels, halts=True, flattens=True)
        case MismatchKind.UNKNOWN_POSITION | MismatchKind.POSITION_MISMATCH:
            return _position(finding, local)
        case MismatchKind.MISSING_STOP | MismatchKind.STOP_SIZE_MISMATCH:
            return _stop(finding, local, mids, quote_waited)


def _cancel(finding: Finding) -> Planned:
    return Planned(finding=finding, action=MismatchAction.CANCELLED, steps=(CancelOrder(order=_order(finding)),))


def _order(finding: Finding) -> OrderView:
    """The order an ``unknown_order`` or ``local_only_order`` finding names."""
    (order,) = finding.orders
    return order


def plan(
    comparison: Comparison,
    local: LocalView,
    mode: Mode,
    mids: Mapping[str, Decimal] | None = None,
    *,
    quote_waited: bool = False,
    recoveries: Mapping[str, Recovery] | None = None,
) -> Plan:
    """What to do about each finding. ``mids`` holds only fresh quotes; ``quote_waited`` is True once the
    actor waited ``RISK_STALE_QUOTE_S`` for the missing ones. ``recoveries`` maps a local only order's
    client order id to its missed fills (``recover``)."""
    mids = mids or {}
    recoveries = recoveries or {}
    if mode is Mode.DEMO:
        return Plan(tuple(Planned(finding=f, action=MismatchAction.NONE) for f in comparison.findings))
    if mode is Mode.REPORT:
        return Plan(tuple(_report_only(f, local, recoveries) for f in comparison.findings))
    planned = [_act(f, local, mids, quote_waited, recoveries) for f in comparison.findings]
    if any(p.halts for p in planned):
        planned = [_skip_stop_fix(p) for p in planned]
    return Plan(tuple(planned))


def _report_only(finding: Finding, local: LocalView, recoveries: Mapping[str, Recovery]) -> Planned:
    if finding.kind is MismatchKind.UNKNOWN_ORDER and not _order(finding).reduce_only:
        return _cancel(finding)
    if finding.kind is MismatchKind.LOCAL_ONLY_ORDER:
        return _local_only(finding, recoveries)  # only corrects the books (AC-11, AC-28)
    if _cache_holds_more(finding, local):
        return _manual(finding, halts=False)  # the executor still closes it; the existing halt stays (AC-28)
    return Planned(finding=finding, action=MismatchAction.NONE)


def _skip_stop_fix(planned: Planned) -> Planned:
    if planned.finding.kind not in (MismatchKind.MISSING_STOP, MismatchKind.STOP_SIZE_MISMATCH) or planned.halts:
        return planned
    return Planned(
        finding=planned.finding,
        action=MismatchAction.FLATTENED,
        halts=True,
        flattens=True,
        details={"why": HALTED_ELSEWHERE},
    )
