"""The market context a strategy reads: what was known at a moment (spec 0009 AC-8 to AC-10).

One ``ContextBook`` per instrument folds the context updates in arrival order and answers
``snapshot(now_ns)``. Known time is always ``ts_init``, so a snapshot depends only on updates with
``ts_init <= now_ns``, the same in a replay and live. Values are ``Decimal`` and times nanosecond
ints; the node adapter converts Nautilus objects before calling ``apply_*``.
"""

from collections import deque
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from enum import StrEnum
from typing import Final

from trading_core.marketdata.hours import SECOND_NS

# Basis is computed in this context, never the thread's default one (AC-8).
BASIS_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_ZERO: Final = Decimal(0)


class ContextError(Exception):
    """Base for every market context error."""


class ContextLookAheadError(ContextError):
    """A snapshot asked for a moment before an update it already holds: the caller would see the future (AC-8)."""

    def __init__(self, now_ns: int, latest_ns: int) -> None:
        super().__init__(f"snapshot at {now_ns} is before the latest applied ts_init {latest_ns}")
        self.now_ns = now_ns
        self.latest_ns = latest_ns


class AggressorSide(StrEnum):
    """The side that traded into the book: SELL means longs were forced out, BUY shorts (AC-10)."""

    BUY = "BUY"
    SELL = "SELL"


class LiquidationKind(StrEnum):
    """Kraken's non fill trade types (AC-3). Only ``LIQUIDATION`` counts in the window sums."""

    LIQUIDATION = "liquidation"
    TERMINATION = "termination"
    BLOCK = "block"


@dataclass(frozen=True)
class MaxAges:
    """How old each value may be before it is stale, in nanoseconds (AC-9)."""

    mark_ns: int
    index_ns: int
    open_interest_ns: int
    funding_ns: int

    @classmethod
    def from_seconds(cls, *, mark: int, index: int, open_interest: int, funding: int) -> MaxAges:
        """The max ages from whole seconds, as the settings and the run file give them."""
        return cls(mark * SECOND_NS, index * SECOND_NS, open_interest * SECOND_NS, funding * SECOND_NS)


@dataclass(frozen=True)
class ContextValue:
    """One value with the ``ts_init`` it became known at, and whether it is past its max age."""

    value: Decimal
    known_ns: int
    is_stale: bool


@dataclass(frozen=True)
class Liquidation:
    """One forced trade as the book holds it (a ``LiquidationTick`` without the instrument)."""

    trade_id: str
    kind: LiquidationKind
    aggressor_side: AggressorSide
    price: Decimal
    size: Decimal
    ts_init: int


@dataclass(frozen=True)
class LiquidationWindow:
    """Liquidations in the window ``(now - window, now]``, notional in exact USD (AC-10).

    ``known`` is false when the feed is not known to be up; a strategy must then treat the sums
    as missing, never as calm. The sums are zero whenever ``known`` is false.
    """

    sold_size: Decimal
    sold_notional: Decimal
    bought_size: Decimal
    bought_notional: Decimal
    last: Liquidation | None
    known: bool


@dataclass(frozen=True)
class MarketContext:
    """What was known at one moment. A value is ``None`` when it was never seen."""

    mark: ContextValue | None
    index: ContextValue | None
    basis: ContextValue | None
    funding_rate: ContextValue | None
    open_interest: ContextValue | None
    next_funding_ns: int | None
    liquidations: LiquidationWindow


@dataclass(frozen=True)
class _Latest:
    value: Decimal
    ts_init: int


class ContextBook:
    """Folds context updates for one instrument and answers ``snapshot(now_ns)`` (AC-8).

    An update older (by ``ts_init``) than the latest of its kind is ignored; one at the same
    ``ts_init`` replaces it, so arrival order decides ties. The max ages, the liquidation window,
    and whether liquidations exist at all for this source are given here; the book reads no settings.
    """

    def __init__(self, max_ages: MaxAges, liquidation_window_ns: int, *, liquidations_available: bool) -> None:
        if liquidation_window_ns <= 0:
            raise ValueError("the liquidation window must be positive")
        self._max_ages = max_ages
        self._window_ns = liquidation_window_ns
        self._liquidations_available = liquidations_available
        self._mark: _Latest | None = None
        self._index: _Latest | None = None
        self._funding: _Latest | None = None
        self._next_funding_ns: int | None = None
        self._open_interest: _Latest | None = None
        self._liquidations: deque[Liquidation] = deque()
        self._last_liquidation: Liquidation | None = None
        self._liquidation_ns: int | None = None  # the newest liquidation tick of any kind
        self._latest_ns: int | None = None

    @property
    def latest_ns(self) -> int | None:
        """The newest ``ts_init`` applied so far, or ``None`` before the first update."""
        return self._latest_ns

    def apply_mark(self, value: Decimal, ts_init: int) -> None:
        """Fold a mark price."""
        self._mark = self._newer(self._mark, value, ts_init)

    def apply_index(self, value: Decimal, ts_init: int) -> None:
        """Fold an index price."""
        self._index = self._newer(self._index, value, ts_init)

    def apply_funding(self, rate: Decimal, next_funding_ns: int | None, ts_init: int) -> None:
        """Fold a funding rate (fractional, per period) and the next funding time it came with."""
        newer = self._newer(self._funding, rate, ts_init)
        if newer is not self._funding:
            self._funding = newer
            self._next_funding_ns = next_funding_ns

    def apply_open_interest(self, value: Decimal, ts_init: int) -> None:
        """Fold an open interest in contracts."""
        self._open_interest = self._newer(self._open_interest, value, ts_init)

    def apply_liquidation(self, liquidation: Liquidation) -> None:
        """Fold one forced trade, then prune rows that can no longer fall in any later window (AC-10)."""
        if self._liquidation_ns is not None and liquidation.ts_init < self._liquidation_ns:
            return
        self._liquidation_ns = liquidation.ts_init
        self._seen(liquidation.ts_init)
        if liquidation.kind is LiquidationKind.LIQUIDATION:
            self._liquidations.append(liquidation)
            self._last_liquidation = liquidation
        self._prune()

    def snapshot(self, now_ns: int) -> MarketContext:
        """What was known at ``now_ns``. Raises ``ContextLookAheadError`` if an applied update is newer."""
        if self._latest_ns is not None and now_ns < self._latest_ns:
            raise ContextLookAheadError(now_ns, self._latest_ns)
        ages = self._max_ages
        mark = _value(self._mark, now_ns, ages.mark_ns)
        index = _value(self._index, now_ns, ages.index_ns)
        open_interest = _value(self._open_interest, now_ns, ages.open_interest_ns)
        return MarketContext(
            mark=mark,
            index=index,
            basis=_basis(mark, index),
            funding_rate=_value(self._funding, now_ns, ages.funding_ns),
            open_interest=open_interest,
            next_funding_ns=self._next_funding_ns,
            liquidations=self._window(now_ns, open_interest),
        )

    def _newer(self, current: _Latest | None, value: Decimal, ts_init: int) -> _Latest | None:
        if current is not None and ts_init < current.ts_init:
            return current
        self._seen(ts_init)
        self._prune()
        return _Latest(value, ts_init)

    def _seen(self, ts_init: int) -> None:
        if self._latest_ns is None or ts_init > self._latest_ns:
            self._latest_ns = ts_init

    def _prune(self) -> None:
        # A row at or before latest - window is out of every window from here on, since a
        # snapshot's now is never before latest. Pruning only here keeps snapshot pure.
        if self._latest_ns is None:
            return
        floor = self._latest_ns - self._window_ns
        rows = self._liquidations
        while rows and rows[0].ts_init <= floor:
            rows.popleft()

    def _window(self, now_ns: int, open_interest: ContextValue | None) -> LiquidationWindow:
        known = self._liquidations_available and open_interest is not None and not open_interest.is_stale
        sold_size = sold_notional = bought_size = bought_notional = _ZERO
        if known:
            floor = now_ns - self._window_ns
            for row in self._liquidations:
                if row.ts_init <= floor:
                    continue
                notional = row.price * row.size
                if row.aggressor_side is AggressorSide.SELL:
                    sold_size += row.size
                    sold_notional += notional
                else:
                    bought_size += row.size
                    bought_notional += notional
        return LiquidationWindow(
            sold_size=sold_size,
            sold_notional=sold_notional,
            bought_size=bought_size,
            bought_notional=bought_notional,
            last=self._last_liquidation,
            known=known,
        )


def _value(latest: _Latest | None, now_ns: int, max_age_ns: int) -> ContextValue | None:
    if latest is None:
        return None
    return ContextValue(latest.value, latest.ts_init, now_ns - latest.ts_init > max_age_ns)


def _basis(mark: ContextValue | None, index: ContextValue | None) -> ContextValue | None:
    """``(mark - index) / index`` from the latest of each; known at the later one, stale if either is."""
    if mark is None or index is None or index.value == 0:
        return None
    value = BASIS_CONTEXT.divide(BASIS_CONTEXT.subtract(mark.value, index.value), index.value)
    return ContextValue(value, max(mark.known_ns, index.known_ns), mark.is_stale or index.is_stale)


type LogValue = str | int | bool | None

_AGE_STEP: Final = Decimal("0.001")


def context_log_fields(context: MarketContext, now_ns: int) -> dict[str, LogValue]:
    """The ``market_context`` log line's fields: Decimals as strings, ages in seconds (AC-11).

    Each value gives ``<name>``, ``<name>_age_s``, and ``<name>_stale``, all ``None`` when never seen.
    """
    fields: dict[str, LogValue] = {}
    values = {
        "mark": context.mark,
        "index": context.index,
        "basis": context.basis,
        "funding_rate": context.funding_rate,
        "open_interest": context.open_interest,
    }
    for name, value in values.items():
        fields[name] = None if value is None else str(value.value)
        fields[f"{name}_age_s"] = None if value is None else _age_s(now_ns - value.known_ns)
        fields[f"{name}_stale"] = None if value is None else value.is_stale
    fields["next_funding_ns"] = context.next_funding_ns
    window = context.liquidations
    fields["liquidations_known"] = window.known
    fields["liquidations_sold_size"] = str(window.sold_size)
    fields["liquidations_sold_notional"] = str(window.sold_notional)
    fields["liquidations_bought_size"] = str(window.bought_size)
    fields["liquidations_bought_notional"] = str(window.bought_notional)
    fields["liquidations_last_ns"] = None if window.last is None else window.last.ts_init
    return fields


def _age_s(age_ns: int) -> str:
    return str((Decimal(age_ns) / SECOND_NS).quantize(_AGE_STEP, rounding=ROUND_HALF_EVEN))
