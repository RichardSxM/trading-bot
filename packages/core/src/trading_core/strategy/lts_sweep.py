"""The liquidity trap detector: a failed break of a recent low or high (spec 0031 AC-13, AC-14).

A long starts when a trade breaks the rolling low. The level then stays frozen. Absorption latches
when the sellers' efficiency rank is high enough, and the signal is a fresh cross of the mid back
above the level inside the timeout. A short is the same thing mirrored: every comparison runs on
``sign * price``, so one code path serves both sides.

Every decision uses only events at or before its ``ts_init``. Events are fed in stream order, book
before trades at one nanosecond. One detector serves one segment and starts cold.
"""

from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from trading_core.marketdata.context import AggressorSide
from trading_core.marketdata.hours import SECOND_NS
from trading_core.marketdata.normalized import NormalizedTrade, TradeKind
from trading_core.strategy.lts_features import (
    WINDOW_1S_NS,
    WINDOW_2S_NS,
    EfficiencyRanks,
    FlowWindows,
    aligned_impact_bps,
    efficiency,
)

_MS_NS: Final = 1_000_000
_LONG: Final = 1
_DIRECTION: Final = {AggressorSide.BUY: 1, AggressorSide.SELL: -1}


class LtsSide(StrEnum):
    """The side a signal would trade."""

    LONG = "long"
    SHORT = "short"


class EndReason(StrEnum):
    """Why a candidate ended without a signal, or a sweep never became one."""

    NO_ABSORPTION = "no_absorption"
    NO_RECLAIM = "no_reclaim"
    INVALIDATED = "invalidated"
    SEGMENT_END = "segment_end"
    COOLDOWN = "cooldown"


class DetectorState(StrEnum):
    """Where the detector is."""

    WARMING = "warming"
    FLAT = "flat"
    SWEPT = "swept"
    ABSORBED = "absorbed"


@dataclass(frozen=True)
class LtsParams:
    """The detector's fixed settings. ``tick`` is the instrument's price increment."""

    tick: Decimal
    lookback_s: int
    sweep_buffer_ticks: int
    reclaim_buffer_ticks: int
    invalidation_buffer_ticks: int
    reclaim_timeout_ms: int
    efficiency_pct_min: int
    horizon_s: int


@dataclass(frozen=True)
class CandidateView:
    """A candidate as the runner sees it. The same object is returned until the candidate ends."""

    side: LtsSide
    level: Decimal
    started_ns: int


@dataclass(frozen=True)
class LtsSignal:
    """A signal, with the flow values measured when absorption latched."""

    side: LtsSide
    ts_init_ns: int
    mid: Decimal
    level: Decimal
    extreme: Decimal
    buy_notional_1s: Decimal
    sell_notional_1s: Decimal
    impact_bps: Decimal
    efficiency: Decimal
    efficiency_rank: Decimal
    started_ns: int
    latched_ns: int


class RollingExtremes:
    """The lowest and highest trade of the last ``lookback_ns``, from monotonic queues."""

    def __init__(self, lookback_ns: int) -> None:
        self._lookback_ns = lookback_ns
        self._first_ns: int | None = None
        self._lows: deque[tuple[int, Decimal]] = deque()
        self._highs: deque[tuple[int, Decimal]] = deque()

    def add(self, ts_ns: int, price: Decimal) -> None:
        """Add a trade, after the checks that must exclude it."""
        if self._first_ns is None:
            self._first_ns = ts_ns
        while self._lows and self._lows[-1][1] >= price:
            self._lows.pop()
        self._lows.append((ts_ns, price))
        while self._highs and self._highs[-1][1] <= price:
            self._highs.pop()
        self._highs.append((ts_ns, price))

    def full(self, now_ns: int) -> bool:
        """Whether a whole lookback has passed since the first trade."""
        return self._first_ns is not None and now_ns - self._first_ns >= self._lookback_ns

    def low(self, now_ns: int) -> Decimal | None:
        """The lowest trade in the window ending just before ``now_ns``, or None if empty."""
        return self._front(self._lows, now_ns)

    def high(self, now_ns: int) -> Decimal | None:
        """The highest trade in the window ending just before ``now_ns``, or None if empty."""
        return self._front(self._highs, now_ns)

    def _front(self, queue: deque[tuple[int, Decimal]], now_ns: int) -> Decimal | None:
        while queue and queue[0][0] <= now_ns - self._lookback_ns:
            queue.popleft()
        return queue[0][1] if queue else None


@dataclass
class _Latch:
    extreme: Decimal
    buy_notional_1s: Decimal
    sell_notional_1s: Decimal
    impact_bps: Decimal
    efficiency: Decimal
    rank: Decimal
    latched_ns: int
    below: bool


@dataclass
class _Candidate:
    sign: int
    view: CandidateView
    timeout_ns: int
    # Both of these are in sign * price space, so a long and a short compare the same way.
    extreme: Decimal
    reclaim: Decimal
    latch: _Latch | None = None


class SweepDetector:
    """One candidate at a time per instrument, driven by ``on_trade``, ``on_mid``, and ``on_forced``."""

    def __init__(self, params: LtsParams) -> None:
        self._params = params
        self._flow = FlowWindows()
        self._ranks = EfficiencyRanks()
        self._rolling = RollingExtremes(params.lookback_s * SECOND_NS)
        self._mid: Decimal | None = None
        self._mids: deque[tuple[int, Decimal]] = deque()
        self._second: int | None = None
        self._now_ns = 0
        self._candidate: _Candidate | None = None
        self._signal_ns: int | None = None
        self.ended: Counter[EndReason] = Counter()

    @property
    def candidate(self) -> CandidateView | None:
        """The open candidate, or None."""
        return None if self._candidate is None else self._candidate.view

    @property
    def state(self) -> DetectorState:
        """Where the detector is now."""
        if self._candidate is not None:
            return DetectorState.ABSORBED if self._candidate.latch is not None else DetectorState.SWEPT
        return DetectorState.FLAT if self._ready(self._now_ns) else DetectorState.WARMING

    def on_trade(self, trade: NormalizedTrade) -> None:
        """A trade: it updates the flow and may start, latch, or end a candidate."""
        ts_ns = trade.ts_init_ns
        self._tick(ts_ns)
        counted = self._flow.add_trade(trade)
        self._expire(ts_ns)
        if not counted:
            return
        if self._candidate is not None:
            self._follow(self._candidate, trade)
        if self._candidate is None:
            self._maybe_start(trade)
        self._rolling.add(ts_ns, trade.price)

    def on_forced(self, trade_id: str, side: AggressorSide, notional: Decimal, ts_ns: int, kind: TradeKind) -> None:
        """A forced trade's own record: its id leaves the flow, in whichever order it and the trade arrive."""
        self._tick(ts_ns)
        self._flow.add_forced(trade_id, side, notional, ts_ns, kind)
        self._expire(ts_ns)

    def on_mid(self, ts_ns: int, mid: Decimal) -> LtsSignal | None:
        """The mid changed: returns a signal when a latched candidate crosses back through its level."""
        self._tick(ts_ns)
        self._mid = mid
        self._mids.append((ts_ns, mid))
        while len(self._mids) >= 2 and self._mids[1][0] <= ts_ns - WINDOW_2S_NS:
            self._mids.popleft()
        self._expire(ts_ns)
        candidate = self._candidate
        if candidate is None or candidate.latch is None:
            return None
        latch = candidate.latch
        if candidate.sign * mid < candidate.reclaim:
            latch.below = True
            return None
        if not latch.below:
            return None
        self._candidate = None
        self._signal_ns = ts_ns
        return LtsSignal(
            side=candidate.view.side,
            ts_init_ns=ts_ns,
            mid=mid,
            level=candidate.view.level,
            extreme=candidate.sign * latch.extreme,
            buy_notional_1s=latch.buy_notional_1s,
            sell_notional_1s=latch.sell_notional_1s,
            impact_bps=latch.impact_bps,
            efficiency=latch.efficiency,
            efficiency_rank=latch.rank,
            started_ns=candidate.view.started_ns,
            latched_ns=latch.latched_ns,
        )

    def finish(self) -> None:
        """The segment ended: an open candidate ends with it."""
        if self._candidate is not None:
            self.ended[EndReason.SEGMENT_END] += 1
            self._candidate = None

    def _ready(self, now_ns: int) -> bool:
        return (
            self._rolling.full(now_ns)
            and self._ranks.warm(AggressorSide.BUY, now_ns)
            and self._ranks.warm(AggressorSide.SELL, now_ns)
        )

    def _tick(self, ts_ns: int) -> None:
        """Called before an event is applied, so a sample for a finished second sees only that second's events."""
        self._now_ns = ts_ns
        second = ts_ns // SECOND_NS
        if self._second is not None and second > self._second:
            boundary = (self._second + 1) * SECOND_NS
            start_mid = self._mid_at(boundary - SECOND_NS)
            if start_mid is not None and self._mid is not None:
                for side, direction in _DIRECTION.items():
                    notional = self._flow.side_notional(side, WINDOW_1S_NS, boundary - 1)
                    if notional:
                        value = efficiency(notional, aligned_impact_bps(direction, start_mid, self._mid))
                        self._ranks.add(side, boundary, value)
        self._second = second
        self._ranks.advance(ts_ns)

    def _mid_at(self, ts_ns: int) -> Decimal | None:
        for mid_ts, mid in reversed(self._mids):
            if mid_ts <= ts_ns:
                return mid
        return None

    def _expire(self, ts_ns: int) -> None:
        candidate = self._candidate
        if candidate is None or ts_ns <= candidate.timeout_ns:
            return
        self.ended[EndReason.NO_ABSORPTION if candidate.latch is None else EndReason.NO_RECLAIM] += 1
        self._candidate = None

    def _follow(self, candidate: _Candidate, trade: NormalizedTrade) -> None:
        price = candidate.sign * trade.price
        if candidate.latch is None:
            candidate.extreme = min(candidate.extreme, price)
            self._try_latch(candidate, trade.ts_init_ns)
        elif price <= candidate.extreme - self._params.invalidation_buffer_ticks * self._params.tick:
            self.ended[EndReason.INVALIDATED] += 1
            self._candidate = None

    def _maybe_start(self, trade: NormalizedTrade) -> None:
        ts_ns = trade.ts_init_ns
        if not self._ready(ts_ns):
            return
        buffer = self._params.sweep_buffer_ticks * self._params.tick
        low, high = self._rolling.low(ts_ns), self._rolling.high(ts_ns)
        if low is not None and trade.price <= low - buffer:
            sign, side, level = _LONG, LtsSide.LONG, low
        elif high is not None and trade.price >= high + buffer:
            sign, side, level = -_LONG, LtsSide.SHORT, high
        else:
            return
        if self._signal_ns is not None and ts_ns - self._signal_ns < self._params.horizon_s * SECOND_NS:
            self.ended[EndReason.COOLDOWN] += 1
            return
        candidate = _Candidate(
            sign=sign,
            view=CandidateView(side, level, ts_ns),
            timeout_ns=ts_ns + self._params.reclaim_timeout_ms * _MS_NS,
            extreme=sign * trade.price,
            reclaim=sign * level + self._params.reclaim_buffer_ticks * self._params.tick,
        )
        self._candidate = candidate
        self._try_latch(candidate, ts_ns)

    def _try_latch(self, candidate: _Candidate, ts_ns: int) -> None:
        if self._mid is None:
            return
        # A long fades sellers, a short fades buyers.
        pressure = AggressorSide.SELL if candidate.sign == _LONG else AggressorSide.BUY
        notional = self._flow.side_notional(pressure, WINDOW_1S_NS, ts_ns)
        start_mid = self._mid_at(ts_ns - WINDOW_1S_NS)
        if not notional or start_mid is None:
            return
        impact = aligned_impact_bps(_DIRECTION[pressure], start_mid, self._mid)
        value = efficiency(notional, impact)
        rank = self._ranks.rank(pressure, value)
        if rank < self._params.efficiency_pct_min:
            return
        candidate.latch = _Latch(
            extreme=candidate.extreme,
            buy_notional_1s=self._flow.side_notional(AggressorSide.BUY, WINDOW_1S_NS, ts_ns),
            sell_notional_1s=self._flow.side_notional(AggressorSide.SELL, WINDOW_1S_NS, ts_ns),
            impact_bps=impact,
            efficiency=value,
            rank=rank,
            latched_ns=ts_ns,
            below=candidate.sign * self._mid < candidate.reclaim,
        )
