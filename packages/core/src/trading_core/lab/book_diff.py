"""A Tardis snapshot block written as level changes against the carried book (spec 0008 AC-1).

Tardis resnapshots the book many times a day. Written as a ``CLEAR`` and fresh levels, each block
would make the engine move every resting order to the front of its queue, so the import writes
only what the block changes: ``DELETE`` for a level the block lacks, ``UPDATE`` for a level whose
size differs, ``ADD`` for a new level. Applied to the carried book, the changes give exactly the
block's book. A block that matches the carried book gives no change at all.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Side = Literal["bid", "ask"]
Action = Literal["add", "update", "delete"]


@dataclass(frozen=True)
class Level:
    """One price level of an L2 book. ``size`` is above zero."""

    side: Side
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class LevelChange:
    """One row of a diff. ``size`` is the level's new size, and 0 for a ``delete``."""

    action: Action
    side: Side
    price: Decimal
    size: Decimal


def _by_side(levels: Iterable[Level]) -> dict[Side, dict[Decimal, Decimal]]:
    book: dict[Side, dict[Decimal, Decimal]] = {"bid": {}, "ask": {}}
    for level in levels:
        if level.size <= 0:
            raise ValueError(f"level {level} has no size")
        if level.price in book[level.side]:
            raise ValueError(f"two {level.side} levels at {level.price}")
        book[level.side][level.price] = level.size
    return book


def diff_book(carried: Iterable[Level], block: Iterable[Level]) -> list[LevelChange]:
    """The level changes that turn ``carried`` into ``block``.

    Bids come first, then asks, each best price first (the order the hour start snapshot uses).
    Raises ``ValueError`` when either book holds an empty level or one price twice on a side.
    """
    old, new = _by_side(carried), _by_side(block)
    changes: list[LevelChange] = []
    for side in ("bid", "ask"):
        before, after = old[side], new[side]
        for price in sorted(before.keys() | after.keys(), reverse=side == "bid"):
            was, now = before.get(price), after.get(price)
            if now is None:
                changes.append(LevelChange("delete", side, price, Decimal(0)))
            elif was is None:
                changes.append(LevelChange("add", side, price, now))
            elif was != now:
                changes.append(LevelChange("update", side, price, now))
    return changes
