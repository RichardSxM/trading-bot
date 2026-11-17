"""Replay parity between two engines of one book (spec 0033 AC-10a), pure.

The reference is the pure book replay; the other engine is the Nautilus book mode or the independent
reimplementation. Both give, for every traded day, the book's equity after that day's trades and each coin's
signed quantity. The pass rule is fixed by the spec:

- every day's return (equity over the day before, from the same start equity) within ``day_bps`` (10 bps);
- the cumulative return over the window within ``cumulative_pp`` percentage points (1);
- the position side (long, short, flat) equal for every coin on every day, except a day on which the
  reference had that coin within ``edge`` (1 %) of its band edge, where a rounding sized difference may
  rightly land on the other side of the band.

The two series must hold exactly the same days; a day in one and not the other is a fail. No I/O, no float.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

DAY_BPS: Final = Decimal(10)
CUMULATIVE_PP: Final = Decimal(1)
EDGE: Final = Decimal("0.01")
_BPS: Final = Decimal(10_000)
_PP: Final = Decimal(100)


@dataclass(frozen=True)
class BookDay:
    """One traded day of one engine: equity after the day's trades and each coin's signed quantity then."""

    day: date
    equity: Decimal
    positions: Mapping[str, Decimal]


@dataclass(frozen=True)
class SideMiss:
    """One coin on one day whose side differs between the engines; ``gap`` is the reference's distance from
    the target as a share of it (None when the day's plan did not judge the coin), and ``excused`` is true
    when that gap was within ``edge`` of the band."""

    day: date
    coin: str
    reference: int
    other: int
    gap: Decimal | None
    excused: bool


@dataclass(frozen=True)
class ParityResult:
    """The compare: the days both engines hold, the days only one holds, the worst daily return gap (in bps)
    and its day, both cumulative returns and their gap (in percentage points), every side miss, and the
    verdict."""

    days: int
    unmatched_days: tuple[date, ...]
    worst_day_bps: Decimal
    worst_day: date | None
    reference_return: Decimal
    other_return: Decimal
    cumulative_gap_pp: Decimal
    side_misses: tuple[SideMiss, ...]
    passed: bool

    @property
    def unexcused(self) -> tuple[SideMiss, ...]:
        """The side misses that count against the pass."""
        return tuple(m for m in self.side_misses if not m.excused)


def _side(qty: Decimal) -> int:
    return 1 if qty > 0 else -1 if qty < 0 else 0


def _returns(days: Sequence[BookDay], start_equity: Decimal) -> list[Decimal]:
    equities = [start_equity, *(d.equity for d in days)]
    return [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]


def compare_books(
    reference: Sequence[BookDay],
    other: Sequence[BookDay],
    start_equity: Decimal,
    band: Decimal,
    gaps: Mapping[date, Mapping[str, Decimal]],
    *,
    day_bps: Decimal = DAY_BPS,
    cumulative_pp: Decimal = CUMULATIVE_PP,
    edge: Decimal = EDGE,
) -> ParityResult:
    """Compare ``other`` with ``reference`` (module docstring). ``gaps`` is the reference's band gaps by day
    (``DayResult.band_gaps``). Raises ``ValueError`` for a start equity at or below 0 or an empty reference."""
    if start_equity <= 0:
        raise ValueError(f"start equity must be above 0, got {start_equity}")
    if not reference:
        raise ValueError("the reference holds no day")
    ref_days = [d.day for d in reference]
    other_days = [d.day for d in other]
    unmatched = tuple(sorted(set(ref_days) ^ set(other_days)))
    if unmatched or ref_days != other_days:
        return ParityResult(
            len(set(ref_days) & set(other_days)),
            unmatched,
            Decimal(0),
            None,
            reference[-1].equity / start_equity - 1,
            (other[-1].equity / start_equity - 1) if other else Decimal(0),
            Decimal(0),
            (),
            False,
        )
    ref_returns = _returns(reference, start_equity)
    other_returns = _returns(other, start_equity)
    worst, worst_day = Decimal(0), None
    for d, r, o in zip(reference, ref_returns, other_returns, strict=True):
        gap_bps = abs(r - o) * _BPS
        if gap_bps > worst or worst_day is None:
            worst, worst_day = gap_bps, d.day
    misses: list[SideMiss] = []
    for ref, oth in zip(reference, other, strict=True):
        for coin in sorted(set(ref.positions) | set(oth.positions)):
            a = _side(ref.positions.get(coin, Decimal(0)))
            b = _side(oth.positions.get(coin, Decimal(0)))
            if a == b:
                continue
            gap = gaps.get(ref.day, {}).get(coin)
            excused = gap is not None and abs(gap - band) <= edge
            misses.append(SideMiss(ref.day, coin, a, b, gap, excused))
    ref_total = reference[-1].equity / start_equity - 1
    other_total = other[-1].equity / start_equity - 1
    cumulative_gap = abs(ref_total - other_total) * _PP
    passed = worst <= day_bps and cumulative_gap <= cumulative_pp and not any(not m.excused for m in misses)
    return ParityResult(
        len(reference),
        (),
        worst,
        worst_day,
        ref_total,
        other_total,
        cumulative_gap,
        tuple(misses),
        passed,
    )
