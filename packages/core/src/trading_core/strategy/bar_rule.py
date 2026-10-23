"""The pure rule contract every hour level candidate implements (spec 0018, *Pure rule contract*).

``BarRuleStrategy`` (``trading_core.node.bar_rule``) owns the order lifecycle, sizing, stops, and the
time stop. A candidate only adds a small ``BarRule``: fed every bar close with the base's ATR, it says
when to enter (with a structural stop) and when its own exit fires. Every signal is a one bar event:
after the base asks ``entry()`` it calls ``consumed()``, whether the entry is then approved, refused,
skipped, or dropped because a position is open. Frozen after the first study (AC-7): a change goes into
a new module.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Protocol

type RuleValue = Decimal | str | int | bool | None


class Side(StrEnum):
    """The side a signal enters on."""

    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class BarInput:
    """One closed bar; ``ts_ns`` is its close time."""

    ts_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class EnterSignal:
    """Enter on ``side`` with the stop where the idea is proven wrong; ``reason`` is the decision's reason."""

    side: Side
    structural_stop: Decimal
    reason: str


@dataclass(frozen=True)
class TradeView:
    """The open trade a rule's exit reads: the average fill, the placed stop, and the bar closes held."""

    side: Side
    entry_px: Decimal
    stop_px: Decimal
    bars_held: int


class BarRule(Protocol):
    """A candidate's entry and exit rules, fed one bar close at a time (spec 0018 AC-1, AC-2, AC-4)."""

    def update(self, bar: BarInput, atr: Decimal | None) -> None:
        """Feed one bar close and the base's ATR (``None`` until ready); called on every bar."""

    def ready(self) -> bool:
        """True once every indicator the rule reads has a value."""

    def entry(self) -> EnterSignal | None:
        """The signal at this bar, a pure read that changes nothing; asked after ``update``."""

    def consumed(self) -> None:
        """Clear the setup that fired, so the signal is never used again."""

    def exit(self, trade: TradeView) -> str | None:
        """The rule's own exit reason at this bar, or ``None``; asked only in a position, after ``update``."""

    def on_flat(self) -> None:
        """Reset the rule's trade state after a position closed (setups follow each child spec)."""

    def values(self) -> dict[str, RuleValue]:
        """The rule's current values for the decision's ``inputs``."""


class BadSignalError(ValueError):
    """A structural stop on the wrong side of the mid (at or above it for a long, at or below for a short)."""


def floored_stop(
    side: Side, structural_stop: Decimal, mid: Decimal, atr: Decimal, mult: Decimal, tick: Decimal
) -> tuple[Decimal, bool]:
    """The stop actually used and whether the ATR floor set it (spec 0018 AC-2).

    A long's stop is the lower of the structural stop and ``mid - mult * ATR``, rounded down to ``tick``;
    a short's the higher of the structural stop and ``mid + mult * ATR``, rounded up. So the stop is never
    closer than ``mult * ATR`` to the mid. Raises ``BadSignalError`` for a structural stop on the wrong
    side of the mid, and ``ValueError`` for a tick, ATR, or multiple that is not positive.
    """
    if tick <= 0 or atr <= 0 or mult <= 0:
        raise ValueError(f"bad stop floor inputs: atr {atr}, mult {mult}, tick {tick}")
    if side == Side.LONG:
        if structural_stop >= mid:
            raise BadSignalError(f"long stop {structural_stop} is not below the mid {mid}")
        floor = mid - mult * atr
        raw, rounding, applied = min(structural_stop, floor), ROUND_FLOOR, floor < structural_stop
    else:
        if structural_stop <= mid:
            raise BadSignalError(f"short stop {structural_stop} is not above the mid {mid}")
        floor = mid + mult * atr
        raw, rounding, applied = max(structural_stop, floor), ROUND_CEILING, floor > structural_stop
    return ((raw / tick).to_integral_value(rounding=rounding) * tick).quantize(tick), applied


def r_target(trade: TradeView, target_r: Decimal) -> Decimal:
    """The price ``target_r`` times the trade's risk away from the entry.

    That is ``entry_px ± target_r * |entry_px - stop_px|``.
    """
    risk = abs(trade.entry_px - trade.stop_px)
    return trade.entry_px + target_r * risk if trade.side == Side.LONG else trade.entry_px - target_r * risk
