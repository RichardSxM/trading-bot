"""Trader slots (spec 0030 AC-12): the names that keep two traders in one env apart.

``main`` is the trader that existed before slots, and every name it gets is exactly today's: the
halt and pause keys come from ``halt.py`` and ``pause.py``, its snapshots keep their bare names,
its consumer group and consumer are ``trader``, and its results carry no ``slot`` field.
``portfolio`` is the second demo trader (``trend_portfolio``), with its own keys, snapshot prefix,
consumer group, and a ``slot=portfolio`` field on every result it appends.

A new module, so it sits in no strategy's version closure. No I/O.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from trading_core.control import halt, pause
from trading_core.control.messages import Fields, field
from trading_core.settings import TradingEnv

SLOT_FIELD: Final = "slot"


class TraderSlot(StrEnum):
    """Which trader a process is: ``main`` (six vote today) or ``portfolio`` (``trend_portfolio``)."""

    MAIN = "main"
    PORTFOLIO = "portfolio"


def pause_key(slot: TraderSlot, env: TradingEnv) -> str:
    """The slot's pause key: ``{env}:trader:pause`` for ``main``, ``{env}:trader:portfolio:pause`` else."""
    if slot is TraderSlot.MAIN:
        return pause.pause_key(env)
    return f"{env}:trader:{slot.value}:pause"


def halt_key(slot: TraderSlot, env: TradingEnv) -> str:
    """The slot's halt key: ``{env}:trader:halt`` for ``main``, ``{env}:trader:portfolio:halt`` else."""
    if slot is TraderSlot.MAIN:
        return halt.halt_key(env)
    return f"{env}:trader:{slot.value}:halt"


def snapshot_name(slot: TraderSlot, base: str) -> str:
    """The slot's snapshot name for ``base``: ``base`` itself for ``main``, ``portfolio_<base>`` else."""
    return base if slot is TraderSlot.MAIN else f"{slot.value}_{base}"


def control_group(slot: TraderSlot) -> str:
    """The consumer group the slot reads ``{env}:control`` in: ``trader`` or ``trader-portfolio``."""
    return "trader" if slot is TraderSlot.MAIN else f"trader-{slot.value}"


def consumer_name(slot: TraderSlot) -> str:
    """The slot's consumer inside its group: ``trader`` or ``trader-portfolio-1``."""
    return "trader" if slot is TraderSlot.MAIN else f"trader-{slot.value}-1"


def group_start(slot: TraderSlot) -> str:
    """Where the slot's group starts when first created: ``0`` for ``main`` (as before), ``$`` for a new slot,
    so it never replays an old command (AC-14)."""
    return "0" if slot is TraderSlot.MAIN else "$"


def result_slot_field(slot: TraderSlot) -> Mapping[str, str]:
    """The fields added to every result the slot appends: none for ``main``, ``slot=portfolio`` else."""
    return {} if slot is TraderSlot.MAIN else {SLOT_FIELD: slot.value}


def result_slot(fields: Fields) -> TraderSlot | None:
    """Which slot appended a result entry: no ``slot`` field is ``main``; an unknown value is None."""
    text = field(fields, SLOT_FIELD)
    if text is None:
        return TraderSlot.MAIN
    try:
        return TraderSlot(text)
    except ValueError:
        return None


def event_key(slot: TraderSlot, key: str) -> str:
    """A ``risk_events.event_key`` for the slot: ``key`` itself for ``main``, ``<key>:portfolio`` else.

    Both traders read the same commands, so an event keyed by a command id (``halt:<id>``, ``resume:<id>``) or
    by a day (``trade_cap:<day>``) would collide on ``(env, event_key)`` without the slot (spec 0030 AC-15).
    """
    return key if slot is TraderSlot.MAIN else f"{key}:{slot.value}"


# The commands the ``portfolio`` slot executes (AC-14); any other gets ``failed: not_for_slot``.
PORTFOLIO_COMMANDS: Final = frozenset({"stop", "resume", "pause", "unpause"})


def executes(slot: TraderSlot, cmd: str) -> bool:
    """True when the slot executes ``cmd`` (a ``ControlCmd`` value): ``main`` runs every command."""
    return slot is TraderSlot.MAIN or cmd in PORTFOLIO_COMMANDS
