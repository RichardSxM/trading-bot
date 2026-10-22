"""The scalping candidates' shared rules (spec 0017 AC-5), pure: the signal, its refusals, and the exits.

A child's signal function returns a ``ScalpSignal`` on a 1 minute bar close. Before the risk guard
sizes it, ``pre_refusal`` checks, in this order: the gate and the entry blocks (as the guard words
them), the spread cap, the reward floor, and that the stop is on the losing side of the mid (the
guard cannot judge an entry whose stop is not). While a position is open, ``target_hit`` says when a
quote reaches the target, and ``time_stop_due`` when the hold ran out.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final

from trading_core.recording.enums import OrderSide
from trading_core.risk.check import BLOCKED_PREFIX, HALTED, PAUSED

SPREAD_CAP: Final = "spread_cap"
TARGET_TOO_CLOSE: Final = "target_too_close"
INVALID_STOP: Final = "invalid_stop"
# The scalping base's own refusals: logged at info, like the guard's gate refusals.
SCALP_REFUSALS: Final = frozenset({SPREAD_CAP, TARGET_TOO_CLOSE, INVALID_STOP})

EXIT_TARGET: Final = "target"
EXIT_TIME_STOP: Final = "time_stop"
EXIT_STOP: Final = "stop"
EXIT_LATE_ENTRY_FILL: Final = "late_entry_fill"
EXIT_STOP_THROUGH: Final = "stop_through"

BPS: Final = Decimal(10_000)
MINUTE_NS: Final = 60_000_000_000

type SignalInputs = dict[str, Decimal | int | str | None]


@dataclass(frozen=True)
class ScalpSignal:
    """An entry a child asks for: its side, the stop and target prices, and the child's own inputs."""

    side: OrderSide
    stop_px: Decimal
    target_px: Decimal
    inputs: SignalInputs = field(default_factory=dict)


def mid_of(bid: Decimal, ask: Decimal) -> Decimal:
    """The mid of a quote."""
    return (bid + ask) / 2


def spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    """The quoted spread in bps of the mid: ``(ask - bid) / mid * 10,000``."""
    return (ask - bid) / mid_of(bid, ask) * BPS


def target_distance_bps(target_px: Decimal, mid: Decimal) -> Decimal:
    """How far the target is from the mid, in bps of the mid."""
    return abs(target_px - mid) / mid * BPS


def round_stop(side: OrderSide, stop_px: Decimal, tick: Decimal) -> Decimal:
    """``stop_px`` on the price grid, away from the entry: a long's down, a short's up."""
    rounding = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
    return (stop_px / tick).to_integral_value(rounding) * tick


def stop_on_losing_side(side: OrderSide, stop_px: Decimal, mid: Decimal) -> bool:
    """True when a long's stop is below the mid, or a short's above it."""
    return stop_px < mid if side == OrderSide.BUY else stop_px > mid


@dataclass(frozen=True, kw_only=True)
class EntryGate:
    """What the base knows when a signal arrives, for ``pre_refusal``."""

    halted: bool
    paused: bool
    blocks: Collection[str]
    bid: Decimal
    ask: Decimal
    max_spread_bps: Decimal | None
    min_target_bps: Decimal | None


def pre_refusal(signal: ScalpSignal, gate: EntryGate) -> str | None:
    """The first refusal before the risk guard, or None: halted, paused, ``blocked:<first>``, spread cap,
    reward floor, then the stop side."""
    if gate.halted:
        return HALTED
    if gate.paused:
        return PAUSED
    if gate.blocks:
        return BLOCKED_PREFIX + sorted(gate.blocks)[0]
    mid = mid_of(gate.bid, gate.ask)
    if gate.max_spread_bps is not None and spread_bps(gate.bid, gate.ask) > gate.max_spread_bps:
        return SPREAD_CAP
    if gate.min_target_bps is not None and target_distance_bps(signal.target_px, mid) < gate.min_target_bps:
        return TARGET_TOO_CLOSE
    if not stop_on_losing_side(signal.side, signal.stop_px, mid):
        return INVALID_STOP
    return None


def target_hit(long: bool, bid: Decimal, ask: Decimal, target_px: Decimal) -> bool:
    """A long's target is hit when the bid is at or above it; a short's when the ask is at or below it."""
    return bid >= target_px if long else ask <= target_px


def stop_through(long: bool, bid: Decimal, ask: Decimal, avg_px: Decimal, stop_px: Decimal) -> bool:
    """True when a stop at ``stop_px`` is already through at the entry fill (spec 0017 AC-18).

    A long's stop is through when the bid or the average fill price is at or below it; a short's
    when the ask or the average fill price is at or above it.
    """
    if long:
        return bid <= stop_px or avg_px <= stop_px
    return ask >= stop_px or avg_px >= stop_px


def time_stop_due(first_fill_ns: int, time_stop_min: int, now_ns: int) -> bool:
    """True once ``time_stop_min`` minutes have passed since the first entry fill."""
    return now_ns >= first_fill_ns + time_stop_min * MINUTE_NS
