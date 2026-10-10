"""Fill rate and markouts per liquidity side (spec 0008 AC-10), over a variant's entry orders.

Only entry orders count (exits, stops, and ``segment_end`` closes do not). An order belongs to
the side of its fills' liquidity (its first fill's, should they differ); an order with no fill
belongs to the side its type implies: post only and limit orders are ``maker``, market orders
are ``taker``. A rejected post only order counts as placed and not filled. Each horizon's markout
is the mean over the side's fills weighted by fill size, leaving out fills whose horizon passed
their segment's end; a horizon with no fill left is None.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_core.lab.markouts import HORIZONS_S
from trading_core.recording.enums import Liquidity

SIDES = (Liquidity.MAKER, Liquidity.TAKER)


@dataclass(frozen=True)
class EntryOrder:
    """One entry order: its id and the side its type implies (``maker`` for limit and post only)."""

    client_order_id: str
    implied: Liquidity


@dataclass(frozen=True)
class EntryFill:
    """One fill of an entry order, with its markouts by horizon (None when left out)."""

    client_order_id: str
    liquidity: Liquidity
    quantity: Decimal
    markouts: Mapping[int, Decimal | None]


@dataclass(frozen=True)
class SideStats:
    """One liquidity side's numbers, as ``run_fill_stats`` and the report hold them."""

    liquidity: Liquidity
    orders_placed: int
    orders_filled: int
    fill_count: int
    fill_rate: Decimal
    # Mean markout by horizon (seconds), and how many fills each horizon left out.
    markouts: Mapping[int, Decimal | None]
    left_out: Mapping[int, int]


def fill_stats(
    orders: Sequence[EntryOrder], fills: Sequence[EntryFill], horizons_s: Sequence[int] = HORIZONS_S
) -> list[SideStats]:
    """The stats of each side (``maker`` then ``taker``) that had at least one entry order.

    Raises ``ValueError`` for a fill of an order not in ``orders``, or a side other than maker or taker.
    """
    by_order: dict[str, list[EntryFill]] = {o.client_order_id: [] for o in orders}
    for fill in fills:
        if fill.client_order_id not in by_order:
            raise ValueError(f"fill of {fill.client_order_id}, which is not an entry order")
        by_order[fill.client_order_id].append(fill)

    placed: dict[Liquidity, list[str]] = {side: [] for side in SIDES}
    for order in orders:
        own = by_order[order.client_order_id]
        side = own[0].liquidity if own else order.implied
        if side not in placed:
            raise ValueError(f"order {order.client_order_id} has no maker or taker side ({side})")
        placed[side].append(order.client_order_id)

    stats: list[SideStats] = []
    for side in SIDES:
        ids = placed[side]
        if not ids:
            continue
        side_fills = [f for coid in ids for f in by_order[coid]]
        filled = sum(1 for coid in ids if by_order[coid])
        means: dict[int, Decimal | None] = {}
        left_out: dict[int, int] = {}
        for h in horizons_s:
            kept = [(f.quantity, value) for f in side_fills if (value := f.markouts.get(h)) is not None]
            left_out[h] = len(side_fills) - len(kept)
            weight = sum((q for q, _ in kept), Decimal(0))
            means[h] = sum((q * v for q, v in kept), Decimal(0)) / weight if weight > 0 else None
        stats.append(
            SideStats(
                liquidity=side,
                orders_placed=len(ids),
                orders_filled=filled,
                fill_count=len(side_fills),
                fill_rate=Decimal(filled) / Decimal(len(ids)),
                markouts=means,
                left_out=left_out,
            )
        )
    return stats
