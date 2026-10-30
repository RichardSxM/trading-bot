"""The live drill's pure rules (spec 0023 AC-22, value sourcing): its steps, its size, and its prices.

``reconcile_drill`` sends orders only for a ``drill_step`` command from ``cli``. It never holds more
than the drill size (the larger of the instrument's ``size_increment`` and ``min_quantity``) and never
more than one position. Both prices sit below the market (it only ever buys) and are rounded down to
the instrument's tick, so the resting buy never crosses and the stop never sits closer than asked.
"""

from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from trading_core.recording.enums import ControlSource

STRATEGY_NAME: Final = "reconcile_drill"
HUNDRED: Final = Decimal(100)


class DrillStep(StrEnum):
    """The steps a ``drill_step`` command may ask for (Interface surface)."""

    REST_ORDER = "rest_order"
    REST_ORDER_AND_DIE = "rest_order_and_die"
    OPEN_POSITION = "open_position"
    FLATTEN = "flatten"


ENTRY_STEPS: Final = frozenset({DrillStep.REST_ORDER, DrillStep.REST_ORDER_AND_DIE, DrillStep.OPEN_POSITION})


class DrillRefusal(StrEnum):
    """Why a ``drill_step`` was refused, the ``failed`` reason it answers with."""

    NOT_DRILL_MODE = "not_drill_mode"
    SOURCE_NOT_CLI = "source_not_cli"
    UNKNOWN_STEP = "unknown_step"
    POSITION_OPEN = "position_open"
    ORDER_OPEN = "order_open"
    NO_INSTRUMENT = "no_instrument"
    NO_QUOTE = "no_quote"
    BLOCKED = "blocked"


def parse_step(text: str | None) -> DrillStep | None:
    """The step named by ``text``, or None for a missing or unknown one."""
    try:
        return None if text is None else DrillStep(text)
    except ValueError:
        return None


def step_refusal(
    step: DrillStep | None, source: ControlSource, *, position_open: bool, order_open: bool
) -> DrillRefusal | None:
    """Why ``step`` must be refused, or None when it may run.

    Only ``cli`` may send one. An entry step needs the drill flat with no order of its own open, so
    the drill never holds more than one position of the drill size. ``flatten`` always runs.
    """
    if source is not ControlSource.CLI:
        return DrillRefusal.SOURCE_NOT_CLI
    if step is None:
        return DrillRefusal.UNKNOWN_STEP
    if step not in ENTRY_STEPS:
        return None
    if position_open:
        return DrillRefusal.POSITION_OPEN
    if order_open:
        return DrillRefusal.ORDER_OPEN
    return None


def drill_size(size_increment: Decimal, min_quantity: Decimal | None) -> Decimal:
    """The drill's only size: the larger of the instrument's ``size_increment`` and ``min_quantity``."""
    if size_increment <= 0:
        raise ValueError("size_increment must be above 0")
    return max(size_increment, min_quantity or Decimal(0))


def round_down(price: Decimal, tick: Decimal) -> Decimal:
    """``price`` rounded down to a multiple of ``tick``."""
    if tick <= 0:
        raise ValueError("tick must be above 0")
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def rest_price(mid: Decimal, distance_pct: Decimal, tick: Decimal) -> Decimal:
    """The resting post only buy: ``mid`` x (1 - ``DRILL_REST_DISTANCE_PCT``), rounded down to the tick."""
    if mid <= 0:
        raise ValueError("mid must be above 0")
    return round_down(mid * (1 - distance_pct / HUNDRED), tick)


def stop_trigger(fill_px: Decimal, stop_pct: Decimal, tick: Decimal) -> Decimal:
    """The long drill position's sell stop: the fill price x (1 - ``DRILL_STOP_PCT``), rounded down to the tick."""
    if fill_px <= 0:
        raise ValueError("fill price must be above 0")
    return round_down(fill_px * (1 - stop_pct / HUNDRED), tick)
