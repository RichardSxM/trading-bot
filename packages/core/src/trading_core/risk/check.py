"""The pure risk check that guards every entry (spec 0006 AC-5).

``check_entry`` returns the first refusal in a fixed order, or approval. It never raises for
a refusal; it raises ``InvalidRiskInput`` only when the inputs cannot be judged (a mid or an
equity at or below zero), and the caller fails closed.
"""

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.recording.enums import Severity

PAUSED: Final = "paused"
POSITION_OPEN: Final = "position_open"
ZERO_QUANTITY: Final = "zero_quantity"
NOTIONAL_CAP: Final = "notional_cap"
LEVERAGE_CAP: Final = "leverage_cap"
BLOCKED_PREFIX: Final = "blocked:"

# Refusals that are the normal state of things, not a limit being hit.
_INFO_REFUSALS: Final = frozenset({PAUSED, POSITION_OPEN})


class InvalidRiskInput(ValueError):
    """The check cannot judge the entry: the mid or the equity is zero or below."""


@dataclass(frozen=True, kw_only=True)
class RiskLimits:
    """The fixed caps: the largest order notional, and open notional over equity after the order."""

    max_order_notional: Decimal
    max_leverage: Decimal


@dataclass(frozen=True, kw_only=True)
class EntryRequest:
    """One entry to judge. ``open_notional`` is the notional already open, before this order."""

    quantity: Decimal
    mid: Decimal
    equity: Decimal
    open_notional: Decimal
    has_position: bool
    paused: bool
    blocks: Collection[str]


@dataclass(frozen=True)
class RiskVerdict:
    """The answer: approved, or refused with the reason (``None`` when approved)."""

    approved: bool
    reason: str | None = None


def check_entry(request: EntryRequest, limits: RiskLimits) -> RiskVerdict:
    """Approve the entry, or refuse it with the first matching reason in this order.

    ``paused``, ``blocked:<reason>`` (the first active block, sorted), ``position_open``,
    ``zero_quantity``, ``notional_cap`` (the order's notional over the cap), ``leverage_cap``
    (open notional after the order, divided by equity, over the cap).
    """
    if request.mid <= 0:
        raise InvalidRiskInput(f"mid must be above zero, got {request.mid}")
    if request.equity <= 0:
        raise InvalidRiskInput(f"equity must be above zero, got {request.equity}")
    if request.paused:
        return RiskVerdict(False, PAUSED)
    if request.blocks:
        return RiskVerdict(False, BLOCKED_PREFIX + sorted(request.blocks)[0])
    if request.has_position:
        return RiskVerdict(False, POSITION_OPEN)
    if request.quantity <= 0:
        return RiskVerdict(False, ZERO_QUANTITY)
    order_notional = request.quantity * request.mid
    if order_notional > limits.max_order_notional:
        return RiskVerdict(False, NOTIONAL_CAP)
    if (request.open_notional + order_notional) / request.equity > limits.max_leverage:
        return RiskVerdict(False, LEVERAGE_CAP)
    return RiskVerdict(True)


def refusal_severity(reason: str) -> Severity:
    """The ``limit_hit`` severity for a refusal: ``info`` when paused, blocked, or positioned, else ``warning``."""
    if reason in _INFO_REFUSALS or reason.startswith(BLOCKED_PREFIX):
        return Severity.INFO
    return Severity.WARNING
