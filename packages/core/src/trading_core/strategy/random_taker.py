"""The random taker's pure rules (spec 0008, *random_taker*), shared with ``maker_probe``.

Every ``interval_s`` seconds on the UTC clock it draws a side from the seed and the boundary
alone, so a draw never depends on earlier draws (and a replay is the same whatever happened
before). It has no edge by design: the loser check (AC-14) expects it to lose fees plus spread.
"""

import random
from typing import Final

from trading_core.recording.enums import OrderSide

NS_PER_S: Final = 1_000_000_000


def next_boundary_ns(now_ns: int, interval_s: int) -> int:
    """The first UTC boundary (a whole multiple of ``interval_s`` seconds) strictly after ``now_ns``."""
    if interval_s <= 0:
        raise ValueError(f"interval must be above zero, got {interval_s}")
    step = interval_s * NS_PER_S
    return (now_ns // step + 1) * step


def draw_side(seed: int, boundary_ns: int) -> OrderSide:
    """``buy`` or ``sell`` with even odds, from ``random.Random(seed ^ boundary_ns)`` only."""
    return OrderSide.BUY if random.Random(seed ^ boundary_ns).random() < 0.5 else OrderSide.SELL  # not crypto
