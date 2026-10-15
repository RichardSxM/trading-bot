"""The pure risk guard every entry goes through (spec 0006 AC-5, spec 0011).

``check_entry`` sizes the entry from its risk and clips it to the caps (or checks a fixed quantity
against them), estimates the liquidation price, and returns the first refusal in a fixed order, or
approval, along with every figure it used. It never raises for a refusal; it raises
``InvalidRiskInput`` only when the inputs cannot be judged, and the caller fails closed.
"""

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.recording.enums import OrderSide, Severity
from trading_core.risk.liquidation import distance_pct, liquidation_price, maintenance_rate
from trading_core.risk.sizing import RISK, SizeBound, floor_to_step, loss_per_unit, risk_quantity, smallest

HALTED: Final = "halted"
PAUSED: Final = "paused"
POSITION_OPEN: Final = "position_open"
FUNDING_STALE: Final = "funding_stale"
ZERO_QUANTITY: Final = "zero_quantity"
NOTIONAL_CAP: Final = "notional_cap"
EXPOSURE_CAP: Final = "exposure_cap"
LEVERAGE_CAP: Final = "leverage_cap"
LIQ_TOO_CLOSE: Final = "liq_too_close"
LIQ_INSIDE_STOP: Final = "liq_inside_stop"
BLOCKED_PREFIX: Final = "blocked:"

# Refusals that are the normal state of things or a feed state, not a limit being hit.
_INFO_REFUSALS: Final = frozenset({HALTED, PAUSED, POSITION_OPEN, FUNDING_STALE})


class InvalidRiskInput(ValueError):
    """The check cannot judge the entry (spec 0011 AC-15): a bad mid, equity, stop, size step, or rate."""


@dataclass(frozen=True, kw_only=True)
class RiskLimits:
    """The caps and the sizing rules (``RISK_*``, spec 0011 AC-19)."""

    max_order_notional: Decimal
    max_leverage: Decimal
    max_total_exposure: Decimal
    risk_per_trade: Decimal
    maintenance_margin_rate: Decimal
    min_liq_distance_pct: Decimal
    liq_stop_multiple: Decimal
    stop_slippage_bps: Decimal


@dataclass(frozen=True, kw_only=True)
class EntryRequest:
    """One entry to judge.

    ``mid`` is the entry price. ``open_notional`` is the notional already open on every instrument,
    before this order (AC-4). A request with ``stop_px`` and no ``fixed_quantity`` is sized (AC-1);
    one with ``fixed_quantity`` is only checked (AC-3). ``funding_rate`` is the relative rate per
    hour, ``None`` when missing or stale; ``funding_periods`` counts the boundaries inside the hold.
    ``instrument_maintenance_rate`` is the instrument's ``margin_maint``, 0 when unset (AC-6).
    """

    side: OrderSide
    mid: Decimal
    equity: Decimal
    open_notional: Decimal
    size_step: Decimal
    taker_fee: Decimal
    instrument_maintenance_rate: Decimal
    funding_periods: int
    has_position: bool
    paused: bool
    halted: bool
    blocks: Collection[str]
    stop_px: Decimal | None = None
    fixed_quantity: Decimal | None = None
    funding_rate: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class GuardFigures:
    """Every figure the guard used (spec 0011 AC-12); ``None`` where a figure does not apply."""

    equity: Decimal
    entry_px: Decimal
    stop_px: Decimal | None
    open_notional: Decimal
    taker_fee: Decimal
    maintenance_rate: Decimal
    funding_rate: Decimal | None
    funding_periods: int
    loss_per_unit: Decimal | None
    risk_quantity: Decimal | None
    quantity: Decimal
    limited_by: str | None
    liq_px: Decimal | None
    liq_distance_pct: Decimal | None

    def as_inputs(self) -> dict[str, Decimal | int | str | None]:
        """The figures as a flat dict, for a decision's ``inputs`` JSON."""
        return dict(vars(self))


@dataclass(frozen=True, kw_only=True)
class RiskVerdict:
    """The answer: approved, or refused with the reason (``None`` when approved), and the sized quantity.

    ``limited_by`` says what set a sized quantity (``risk`` or a cap reason); ``None`` for a fixed quantity.
    """

    approved: bool
    reason: str | None
    quantity: Decimal
    limited_by: str | None
    figures: GuardFigures


def _sign(side: OrderSide) -> int:
    return 1 if side == OrderSide.BUY else -1


def _validate(request: EntryRequest, rate: Decimal) -> None:
    if request.mid <= 0:
        raise InvalidRiskInput(f"mid must be above zero, got {request.mid}")
    if request.equity <= 0:
        raise InvalidRiskInput(f"equity must be above zero, got {request.equity}")
    if request.size_step <= 0:
        raise InvalidRiskInput(f"size step must be above zero, got {request.size_step}")
    if request.open_notional < 0 or request.taker_fee < 0 or request.funding_periods < 0:
        raise InvalidRiskInput("open notional, taker fee, and funding periods must not be negative")
    if not 0 < rate < 1:
        raise InvalidRiskInput(f"maintenance rate must be in (0, 1), got {rate}")
    if request.stop_px is not None and (request.stop_px - request.mid) * _sign(request.side) >= 0:
        raise InvalidRiskInput(f"stop {request.stop_px} is not on the losing side of the mid {request.mid}")
    if request.stop_px is None and request.fixed_quantity is None:
        raise InvalidRiskInput("a request needs a stop price to be sized, or a fixed quantity")


def _cap_bounds(request: EntryRequest, limits: RiskLimits) -> list[SizeBound]:
    mid, step, open_notional = request.mid, request.size_step, request.open_notional
    return [
        SizeBound(floor_to_step(limits.max_order_notional / mid, step), NOTIONAL_CAP),
        SizeBound(floor_to_step((limits.max_total_exposure - open_notional) / mid, step), EXPOSURE_CAP),
        SizeBound(floor_to_step((limits.max_leverage * request.equity - open_notional) / mid, step), LEVERAGE_CAP),
    ]


def _fixed_refusal(request: EntryRequest, quantity: Decimal, limits: RiskLimits) -> str | None:
    if quantity <= 0:
        return ZERO_QUANTITY
    order_notional = quantity * request.mid
    if order_notional > limits.max_order_notional:
        return NOTIONAL_CAP
    if request.open_notional + order_notional > limits.max_total_exposure:
        return EXPOSURE_CAP
    if (request.open_notional + order_notional) / request.equity > limits.max_leverage:
        return LEVERAGE_CAP
    return None


def _gate_refusal(request: EntryRequest) -> str | None:
    if request.halted:
        return HALTED
    if request.paused:
        return PAUSED
    if request.blocks:
        return BLOCKED_PREFIX + sorted(request.blocks)[0]
    if request.has_position:
        return POSITION_OPEN
    if request.funding_periods > 0 and request.funding_rate is None:
        return FUNDING_STALE
    return None


def check_entry(request: EntryRequest, limits: RiskLimits) -> RiskVerdict:
    """Size the entry and approve it, or refuse it with the first matching reason in this order (AC-11).

    ``halted``, ``paused``, ``blocked:<reason>`` (the first active block, sorted), ``position_open``,
    ``funding_stale``, ``zero_quantity``, ``notional_cap``, ``exposure_cap``, ``leverage_cap``,
    ``liq_too_close``, ``liq_inside_stop``. A sized entry is clipped to the caps, so it is refused by
    a cap only when that cap leaves less than one size step.
    """
    rate = maintenance_rate(request.instrument_maintenance_rate, limits.maintenance_margin_rate)
    _validate(request, rate)
    per_unit: Decimal | None = None
    sized: Decimal | None = None
    limited_by: str | None = None
    if request.fixed_quantity is not None:
        quantity = request.fixed_quantity
        size_refusal = _fixed_refusal(request, quantity, limits)
    else:
        assert request.stop_px is not None  # _validate
        per_unit = loss_per_unit(
            cost_sign=_sign(request.side),
            entry_px=request.mid,
            stop_px=request.stop_px,
            slippage_bps=limits.stop_slippage_bps,
            taker_fee=request.taker_fee,
            funding_rate=request.funding_rate,
            funding_periods=request.funding_periods,
        )
        sized = risk_quantity(request.equity, limits.risk_per_trade, per_unit, request.size_step)
        bound = smallest(_cap_bounds(request, limits), SizeBound(sized, RISK))
        quantity, limited_by = bound.quantity, bound.limited_by
        size_refusal = None if quantity > 0 else (ZERO_QUANTITY if limited_by == RISK else limited_by)

    liq_px: Decimal | None = None
    if quantity > 0:
        liq_px = liquidation_price(
            request.side,
            quantity,
            request.mid,
            request.equity,
            request.open_notional,
            request.taker_fee * quantity * request.mid,
            rate,
        )
    liq_pct = distance_pct(request.mid, liq_px)

    reason = _gate_refusal(request) or size_refusal
    if reason is None and liq_pct is not None and liq_px is not None:
        if liq_pct < limits.min_liq_distance_pct:
            reason = LIQ_TOO_CLOSE
        elif request.stop_px is not None and abs(request.mid - liq_px) < limits.liq_stop_multiple * abs(
            request.mid - request.stop_px
        ):
            reason = LIQ_INSIDE_STOP

    figures = GuardFigures(
        equity=request.equity,
        entry_px=request.mid,
        stop_px=request.stop_px,
        open_notional=request.open_notional,
        taker_fee=request.taker_fee,
        maintenance_rate=rate,
        funding_rate=request.funding_rate,
        funding_periods=request.funding_periods,
        loss_per_unit=per_unit,
        risk_quantity=sized,
        quantity=quantity,
        limited_by=limited_by,
        liq_px=liq_px,
        liq_distance_pct=liq_pct,
    )
    return RiskVerdict(
        approved=reason is None, reason=reason, quantity=quantity, limited_by=limited_by, figures=figures
    )


def refusal_severity(reason: str) -> Severity:
    """The ``limit_hit`` severity: ``info`` when halted, paused, blocked, positioned, or funding stale, else warning."""
    if reason in _INFO_REFUSALS or reason.startswith(BLOCKED_PREFIX):
        return Severity.INFO
    return Severity.WARNING
