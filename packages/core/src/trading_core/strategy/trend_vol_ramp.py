"""``trend_vol_ramp``'s pure rule (spec 0034 AC-1, AC-2): scale in entries and a wide volatility stop.

``ramp_target`` turns a coin's full target into the target for one rebalance round: a new or flipped position
grows by at most ``|full| / steps`` per round, and only when the round may grow (the daily run); a cut goes
to the full target at once. ``stop_hit`` says when a position has moved ``stop_vols`` daily volatilities
against its entry, and ``stop_holds`` says whether a stopped coin must stay flat given the daily signals
seen since the stop. No I/O, no Nautilus import, and no float.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RampParams:
    """The ramp's settings: how many daily rounds build a full position, and the stop's width in daily vols."""

    steps: int = 3
    stop_vols: Decimal = Decimal(8)

    def __post_init__(self) -> None:
        """Refuse ``steps`` below 1 or ``stop_vols`` at or below 0."""
        if self.steps < 1:
            raise ValueError(f"steps must be at least 1, got {self.steps}")
        if self.stop_vols <= 0:
            raise ValueError(f"stop_vols must be above 0, got {self.stop_vols}")


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def ramp_target(full: Decimal, current: Decimal, steps: int, may_grow: bool) -> Decimal:
    """The working target for one round (both values signed USD notionals).

    A target of 0 is 0. A flat position, or one on the other side, gets ``sign(full) x |full| / steps`` when
    the round may grow, else 0. A position on ``full``'s side at or past it gets ``full`` (a cut at once); one
    below it grows by ``|full| / steps`` (never past ``full``) when the round may grow, else holds at
    ``current``.
    """
    if full == 0:
        return Decimal(0)
    side = _sign(full)
    step = abs(full) / steps
    if _sign(current) != side:
        return side * step if may_grow else Decimal(0)
    if abs(current) >= abs(full):
        return full
    if not may_grow:
        return current
    return side * min(abs(full), abs(current) + step)


def stop_hit(side: int, entry_px: Decimal, mark: Decimal, entry_vol: Decimal, stop_vols: Decimal) -> bool:
    """True when ``side x (mark / entry_px - 1) <= -stop_vols x entry_vol`` (side +1 long, -1 short).

    An ``entry_vol`` of 0 never hits. Raises ``ValueError`` on an entry price or mark at or below 0, or a side
    other than +1 or -1.
    """
    if side not in (1, -1):
        raise ValueError(f"side must be 1 or -1, got {side}")
    if entry_px <= 0 or mark <= 0:
        raise ValueError(f"entry price and mark must be above 0, got {entry_px}, {mark}")
    if entry_vol == 0:
        return False
    return side * (mark / entry_px - 1) <= -stop_vols * entry_vol


def stop_holds(signals: Sequence[int | None], stopped_side: int) -> bool:
    """True while every daily signal since the stop (oldest first; None is not eligible) equals
    ``stopped_side``. An empty list holds."""
    return all(signal == stopped_side for signal in signals)
