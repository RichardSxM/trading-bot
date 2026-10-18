"""The variants of a replay batch (spec 0008 AC-6): ``base`` plus one variant per flag, each changing one thing.

``--queue-bound`` adds ``queue=optimistic``, ``--sweep-latency`` adds ``latency=0`` and
``latency=4x``, and ``--pair-entry`` adds ``entry=taker``. Flags never combine (no cross
product). A variant that equals ``base`` or an earlier variant is dropped, so ``--sweep-latency``
with a latency of 0 adds nothing (both of its variants equal ``base``).

A candle batch (spec 0014 AC-7) has its own variants: ``base``, the ``--sweep-slippage`` pair, or a
``[strategy.grid]`` cross product (``expand_candle_variants``).
"""

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

BASE: Final = "base"
TAKER: Final = "taker"
LATENCY_SWEEP_FACTOR: Final = 4
# A candle batch's grid gives at most this many variants (spec 0014 AC-7).
MAX_GRID_VARIANTS: Final = 16
SLIPPAGE_SWEEP_FACTOR: Final = 2

type GridValue = int | str | bool


class VariantError(ValueError):
    """A variant flag that does not fit the run (``--pair-entry`` without a maker entry to pair)."""


@dataclass(frozen=True)
class VariantSpec:
    """One variant: its name and the values it replays with.

    ``entry_mode`` is None for a strategy without one; ``file_name`` is the name with ``=`` as ``-``.
    """

    name: str
    latency_ms: int
    queue_position: bool
    entry_mode: str | None = None

    @property
    def file_name(self) -> str:
        """The name used for the variant's report files (``latency=4x`` → ``latency-4x``)."""
        return self.name.replace("=", "-")

    def values(self) -> tuple[int, bool, str | None]:
        """What the variant replays with; two variants with equal values are the same run."""
        return (self.latency_ms, self.queue_position, self.entry_mode)


def expand_variants(
    latency_ms: int,
    entry_mode: str | None,
    *,
    queue_bound: bool = False,
    sweep_latency: bool = False,
    pair_entry: bool = False,
) -> list[VariantSpec]:
    """``base`` first, then each flag's variants in the order of the flags above, duplicates dropped.

    ``entry_mode`` is the base's (None when the strategy has no ``entry_mode`` field). Raises
    ``VariantError`` for ``pair_entry`` when there is no entry mode or it is already ``taker``.
    """
    if latency_ms < 0:
        raise ValueError(f"latency_ms must be 0 or more, got {latency_ms}")
    if pair_entry and entry_mode is None:
        raise VariantError("--pair-entry: the strategy has no entry_mode setting")
    if pair_entry and entry_mode == TAKER:
        raise VariantError('--pair-entry: the run file already sets entry_mode = "taker"')
    base = VariantSpec(BASE, latency_ms, True, entry_mode)
    candidates: list[VariantSpec] = []
    if queue_bound:
        candidates.append(VariantSpec("queue=optimistic", latency_ms, False, entry_mode))
    if sweep_latency:
        candidates.append(VariantSpec("latency=0", 0, True, entry_mode))
        candidates.append(VariantSpec("latency=4x", latency_ms * LATENCY_SWEEP_FACTOR, True, entry_mode))
    if pair_entry:
        candidates.append(VariantSpec("entry=taker", latency_ms, True, TAKER))
    variants = [base]
    for candidate in candidates:
        if all(candidate.values() != kept.values() for kept in variants):
            variants.append(candidate)
    return variants


@dataclass(frozen=True)
class CandleVariantSpec:
    """One variant of a candle backtest (spec 0014 AC-7): its name, slippage, and grid values (``{}`` for none)."""

    name: str
    slippage_bps: Decimal
    grid_point: Mapping[str, GridValue] = field(default_factory=dict)


def grid_size(grid: Mapping[str, Sequence[GridValue]]) -> int:
    """How many variants ``grid`` gives (the product of its list lengths; 0 for an empty grid)."""
    if not grid:
        return 0
    size = 1
    for values in grid.values():
        size *= len(values)
    return size


def expand_candle_variants(
    slippage_bps: Decimal,
    grid: Mapping[str, Sequence[GridValue]],
    *,
    sweep_slippage: bool = False,
) -> list[CandleVariantSpec]:
    """A candle batch's variants in plan order (spec 0014 AC-7).

    No grid and no flag: ``base`` alone. A grid replaces ``base`` with ``grid-1``, ``grid-2``, … over
    the cross product of its lists, keys in sorted order and each list in file order (at most
    ``MAX_GRID_VARIANTS``). ``sweep_slippage`` adds ``slippage=0`` and ``slippage=2x`` after ``base``,
    and nothing when ``slippage_bps`` is 0. Raises ``VariantError`` for a grid with the sweep, an
    empty list, or too many points.
    """
    if slippage_bps < 0:
        raise ValueError(f"slippage_bps must be 0 or more, got {slippage_bps}")
    if grid:
        if sweep_slippage:
            raise VariantError("--sweep-slippage cannot be combined with [strategy.grid]")
        empty = sorted(k for k, values in grid.items() if not values)
        if empty:
            raise VariantError(f"[strategy.grid] lists must not be empty: {', '.join(empty)}")
        size = grid_size(grid)
        if size > MAX_GRID_VARIANTS:
            raise VariantError(f"[strategy.grid] gives {size} variants, at most {MAX_GRID_VARIANTS} are allowed")
        keys = sorted(grid)
        return [
            CandleVariantSpec(f"grid-{n}", slippage_bps, dict(zip(keys, point, strict=True)))
            for n, point in enumerate(itertools.product(*(grid[k] for k in keys)), start=1)
        ]
    variants = [CandleVariantSpec(BASE, slippage_bps)]
    if sweep_slippage and slippage_bps > 0:
        variants.append(CandleVariantSpec("slippage=0", Decimal(0)))
        variants.append(CandleVariantSpec("slippage=2x", slippage_bps * SLIPPAGE_SWEEP_FACTOR))
    return variants
