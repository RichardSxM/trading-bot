"""The flow and absorption features of the liquidity trap scalper (spec 0031 AC-10 to AC-12).

Pure: every input is a value handed in, and every time is a ``ts_init`` in nanoseconds. A window of
length w ending at t holds the events with ``t - w < ts <= t``.
"""

import bisect
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Final

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.context_feed import RecentIds
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.normalized import NormalizedTrade, TradeKind

WINDOW_250MS_NS: Final = 250_000_000
WINDOW_1S_NS: Final = SECOND_NS
WINDOW_2S_NS: Final = 2 * SECOND_NS
BPS: Final = Decimal(10_000)
RANK_KEEP_NS: Final = HOUR_NS
RANK_MIN_SAMPLES: Final = 500
_EFFICIENCY_FLOOR_BPS: Final = Decimal("0.1")
_DEPTH_FLOOR: Final = Decimal("0.00000001")


@dataclass(frozen=True)
class FlowSnapshot:
    """The trade flow of one window. The ratio and size fields are None when the window is empty."""

    buy_notional: Decimal
    sell_notional: Decimal
    delta: Decimal
    total: Decimal
    imbalance: Decimal | None
    buy_count: int
    sell_count: int
    median_notional: Decimal | None
    largest_notional: Decimal | None
    arrivals_per_s: Decimal
    liquidation_buy: Decimal
    liquidation_sell: Decimal


@dataclass(frozen=True)
class _Print:
    ts_ns: int
    trade_id: str
    side: AggressorSide
    notional: Decimal


class FlowWindows:
    """Aggressor flow over the last 2 s, with forced trades left out in whichever order they arrive."""

    def __init__(self) -> None:
        self._prints: deque[_Print] = deque()
        self._liquidations: deque[_Print] = deque()
        self._forced = RecentIds()

    def add_trade(self, trade: NormalizedTrade) -> bool:
        """Add a trade to the flow; False when it is forced, so not part of the flow."""
        if trade.kind is not TradeKind.FILL:
            self.add_forced(trade.trade_id, trade.aggressor, trade.notional, trade.ts_init_ns, trade.kind)
            return False
        self._evict(trade.ts_init_ns)
        if trade.trade_id in self._forced:
            return False
        self._prints.append(_Print(trade.ts_init_ns, trade.trade_id, trade.aggressor, trade.notional))
        return True

    def add_forced(self, trade_id: str, side: AggressorSide, notional: Decimal, ts_ns: int, kind: TradeKind) -> None:
        """Remember a forced trade's id once, and take it out of the flow if it was already counted.

        Only a liquidation adds to the liquidation notional.
        """
        self._evict(ts_ns)
        if not self._forced.add(trade_id):
            return
        for counted in self._prints:
            if counted.trade_id == trade_id:
                self._prints.remove(counted)
                break
        if kind is TradeKind.LIQUIDATION:
            self._liquidations.append(_Print(ts_ns, trade_id, side, notional))

    def side_notional(self, side: AggressorSide, window_ns: int, now_ns: int) -> Decimal:
        """The side's aggressor notional over the window ending at ``now_ns``."""
        return sum(
            (p.notional for p in self._prints if p.side is side and now_ns - window_ns < p.ts_ns <= now_ns),
            Decimal(0),
        )

    def snapshot(self, window_ns: int, now_ns: int) -> FlowSnapshot:
        """The full flow figures of the window ending at ``now_ns``."""
        inside = [p for p in self._prints if now_ns - window_ns < p.ts_ns <= now_ns]
        buys = [p.notional for p in inside if p.side is AggressorSide.BUY]
        sells = [p.notional for p in inside if p.side is AggressorSide.SELL]
        buy = sum(buys, Decimal(0))
        sell = sum(sells, Decimal(0))
        total = buy + sell
        notionals = [p.notional for p in inside]
        forced = [p for p in self._liquidations if now_ns - window_ns < p.ts_ns <= now_ns]
        return FlowSnapshot(
            buy_notional=buy,
            sell_notional=sell,
            delta=buy - sell,
            total=total,
            imbalance=(buy - sell) / total if total else None,
            buy_count=len(buys),
            sell_count=len(sells),
            median_notional=median(notionals) if notionals else None,
            largest_notional=max(notionals) if notionals else None,
            arrivals_per_s=Decimal(len(inside)) * SECOND_NS / window_ns,
            liquidation_buy=sum((p.notional for p in forced if p.side is AggressorSide.BUY), Decimal(0)),
            liquidation_sell=sum((p.notional for p in forced if p.side is AggressorSide.SELL), Decimal(0)),
        )

    def _evict(self, now_ns: int) -> None:
        cutoff = now_ns - WINDOW_2S_NS
        for queue in (self._prints, self._liquidations):
            while queue and queue[0].ts_ns <= cutoff:
                queue.popleft()


def aligned_impact_bps(direction: int, mid_start: Decimal, mid_end: Decimal) -> Decimal:
    """The mid's move in the pressure's own direction (+1 buying, -1 selling), in bps."""
    return direction * (mid_end - mid_start) / mid_start * BPS


def efficiency(notional: Decimal, impact_bps: Decimal) -> Decimal:
    """Pressure notional per bp of impact; a move against the pressure counts as strong absorption."""
    return notional / (max(Decimal(0), impact_bps) + _EFFICIENCY_FLOOR_BPS)


def replenishment_ratio(before: Decimal, minimum: Decimal, after: Decimal) -> Decimal:
    """How much of the depth before the sweep came back after its minimum."""
    return max(Decimal(0), after - minimum) / max(before, _DEPTH_FLOOR)


class EfficiencyRanks:
    """The trailing 60 minutes of one second efficiency samples per side, cold until 60 minutes and 500 samples."""

    def __init__(self) -> None:
        self._sorted: dict[AggressorSide, list[Decimal]] = {side: [] for side in AggressorSide}
        self._order: dict[AggressorSide, deque[tuple[int, Decimal]]] = {side: deque() for side in AggressorSide}
        self._first_ns: int | None = None

    def advance(self, now_ns: int) -> None:
        """Move time on: the first call starts the warm up, and samples older than 60 minutes go."""
        if self._first_ns is None:
            self._first_ns = now_ns
        for side, order in self._order.items():
            while order and order[0][0] <= now_ns - RANK_KEEP_NS:
                _, value = order.popleft()
                values = self._sorted[side]
                del values[bisect.bisect_left(values, value)]

    def add(self, side: AggressorSide, ts_ns: int, value: Decimal) -> None:
        """Store one sample for ``side``."""
        self._order[side].append((ts_ns, value))
        bisect.insort(self._sorted[side], value)

    def count(self, side: AggressorSide) -> int:
        """How many samples ``side`` holds now."""
        return len(self._sorted[side])

    def warm(self, side: AggressorSide, now_ns: int) -> bool:
        """Whether ``side`` has had 60 minutes of history and holds at least 500 samples."""
        if self._first_ns is None or now_ns - self._first_ns < RANK_KEEP_NS:
            return False
        return len(self._sorted[side]) >= RANK_MIN_SAMPLES

    def rank(self, side: AggressorSide, value: Decimal) -> Decimal:
        """The percentage of stored samples at or below ``value``; 0 when there are none. ``value`` is never stored."""
        values = self._sorted[side]
        if not values:
            return Decimal(0)
        return Decimal(bisect.bisect_right(values, value)) * 100 / len(values)


class DepthTrack:
    """Displayed depth through one candidate: before the sweep, its minimum, and the latest reading."""

    def __init__(self, before: Decimal, ts_ns: int) -> None:
        self.before = before
        self.minimum = before
        self.after = before
        self._minimum_ns = ts_ns
        self._recovered_ns: int | None = ts_ns

    def update(self, depth: Decimal, ts_ns: int) -> None:
        """Take a new depth reading; a new minimum restarts the wait for recovery."""
        self.after = depth
        if depth < self.minimum:
            self.minimum = depth
            self._minimum_ns = ts_ns
            self._recovered_ns = None
        if self._recovered_ns is None and depth >= self.before:
            self._recovered_ns = ts_ns

    @property
    def ratio(self) -> Decimal:
        """The replenishment ratio of the readings so far."""
        return replenishment_ratio(self.before, self.minimum, self.after)

    @property
    def recovery_ns(self) -> int | None:
        """Nanoseconds from the minimum until depth was back to at least ``before``, or None if it never was."""
        return None if self._recovered_ns is None else self._recovered_ns - self._minimum_ns
