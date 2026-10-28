"""The candle stream: stored 1 minute candles as the engine's data, one minute per chunk (spec 0014 AC-9).

Each traded minute becomes four path points (``trading_core.lab.candle_path``), each a
``TradeTick`` then a ``QuoteTick`` at the same ``ts_init``; a quiet minute is one quote. The
extremes' order follows the strategy's net position, which ``PositionSideFlag`` copies from the
engine's position events into a ``SideFlag`` the stream reads as it generates each minute.
Funding rows merge in by ``ts_init``, before the point at the same nanosecond. Nothing is emitted
before the range's first trade minute, and after the last minute one final quote repeats the last
close, for the range end close. At most one month of candles is held in memory.

Several coins in one engine (spec 0022 AC-7) go through ``merged_chunks``: each coin's minute is
built only when due, funding rows go in at their charge time, and equal times follow sorted ids.
"""

import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import FundingRateUpdate, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.events import PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from trading_core.catalog.candles import CandleTree
from trading_core.lab.candle_path import POINT_OFFSETS_NS, QUOTE_SIZE, PathPoint, final_point, path_points
from trading_core.lab.candles import FUNDING_CARRY_MAX_NS, CandleRow, FundingRow, Month

log = logging.getLogger(__name__)

POSITION_TOPIC: Final = "events.position.*"


@dataclass
class SideFlag:
    """The strategy's net position side as the stream reads it: +1 long, -1 short, 0 flat."""

    side: int = 0
    changed_ns: int = 0


class PositionSideFlagConfig(ActorConfig, frozen=True):
    """No settings: the flag object is passed to the constructor."""


class PositionSideFlag(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Copies every position event's side into its coin's ``SideFlag`` (AC-9; gate G-3: at most one point
    late), so each coin's slippage side follows only its own position (spec 0022 AC-4)."""

    def __init__(self, flags: Mapping[InstrumentId, SideFlag]) -> None:
        super().__init__(PositionSideFlagConfig())
        self.flags = dict(flags)

    def on_start(self) -> None:
        """Subscribe to the position events: a plain actor gets none by default."""
        self.msgbus.subscribe(topic=POSITION_TOPIC, handler=self._on_position)

    def _on_position(self, event: object) -> None:
        if isinstance(event, (PositionOpened, PositionChanged, PositionClosed)):
            flag = self.flags[event.instrument_id]
            qty = event.signed_qty
            flag.side = 0 if qty == 0 else (1 if qty > 0 else -1)
            flag.changed_ns = event.ts_event


MonthDone = Callable[[Month, int, float], None]


class CandleStream:
    """The range ``start_ns``..``end_ns`` of one candles tree, as engine data, at one slippage."""

    def __init__(
        self,
        tree: CandleTree,
        start_ns: int,
        end_ns: int,
        slippage_bps: Decimal,
        flag: SideFlag,
        *,
        on_month: MonthDone | None = None,
    ) -> None:
        self._tree = tree
        self._start = start_ns
        self._end = end_ns
        self._slippage = slippage_bps
        self._flag = flag
        self._on_month = on_month
        instrument = tree.instrument
        self._instrument_id = instrument.id
        self._price_precision = instrument.price_precision
        self._size_precision = instrument.size_precision
        self._tick = instrument.price_increment.as_decimal()
        self._step = instrument.size_increment.as_decimal()
        self._quote_size = Quantity(QUOTE_SIZE, instrument.size_precision)
        self.events = 0
        self.last_point: PathPoint | None = None
        self.first_trade_ns: int | None = None
        # The merge state (spec 0022 AC-7): funding rows not yet sent, the candles left, the next one.
        self._pending: list[FundingRow] = []
        self._cursor: Iterator[CandleRow] = iter(())
        self._next: CandleRow | None = None
        self._final_sent = False

    def months(self) -> list[Month]:
        """The months whose candles or funding the range touches, oldest first."""
        months = [Month.of_ns(self._start)]
        while months[-1] < Month.of_ns(self._end - 1):
            months.append(months[-1].next())
        return months

    def last_point_ns(self) -> int | None:
        """When the range's last point is (its last stored minute's open + 59.999 s, traded or quiet),
        known before streaming so the range end close can be armed; None when the range has no candle."""
        for month in reversed(self.months()):
            inside = [r for r in self._tree.read_bars(month) if self._start <= r.open_ns and r.ts_ns <= self._end]
            if inside:
                return inside[-1].open_ns + POINT_OFFSETS_NS[-1]
        return None

    def warmup(self) -> list[FundingRateUpdate]:
        """The latest funding row with ``ts_init`` in ``[start - FUNDING_CARRY_MAX, start]``, for
        ``warm_market_context`` (AC-6); empty when there is none."""
        rows = [
            r
            for month in {Month.of_ns(self._start - FUNDING_CARRY_MAX_NS), Month.of_ns(self._start)}
            for r in self._tree.read_funding(month)
            if self._start - FUNDING_CARRY_MAX_NS <= r.charge_ns <= self._start
        ]
        return [self._funding(max(rows, key=lambda r: r.charge_ns))] if rows else []

    def chunks(self) -> Iterator[list[Data]]:
        """One list per minute, in ``ts_init`` order, then the final quote."""
        pending: list[FundingRow] = []
        for month in self.months():
            started = time.monotonic()
            events_before = self.events
            pending += [r for r in self._tree.read_funding(month) if self._start < r.charge_ns < self._end]
            for candle in self._tree.read_bars(month):
                if candle.open_ns < self._start or candle.ts_ns > self._end:
                    continue
                if self.first_trade_ns is None:
                    if candle.quiet:
                        continue
                    self.first_trade_ns = candle.open_ns
                points = path_points(candle, self._flag.side, self._slippage, self._tick, self._step)
                chunk: list[Data] = []
                while pending and pending[0].charge_ns <= points[0].ts_ns:
                    chunk.append(self._funding(pending.pop(0)))
                for point in points:
                    chunk.extend(self._point(point))
                self.last_point = points[-1]
                self.events += len(chunk)
                yield chunk
            if self._on_month is not None:
                self._on_month(month, self.events - events_before, time.monotonic() - started)
        if self.last_point is not None:
            # Rows after the last minute (a gap at the range end) still reach the strategy, then the final quote.
            last = final_point(self.last_point)
            final = [self._funding(r) for r in pending if r.charge_ns <= last.ts_ns] + self._point(last)
            self.events += len(final)
            yield final

    @property
    def final_ts_ns(self) -> int | None:
        """When the final quote is, once the stream has run."""
        return None if self.last_point is None else final_point(self.last_point).ts_ns

    # One coin of a merged stream (spec 0022 AC-7) ----------------------------------------------

    def _candles(self) -> Iterator[CandleRow]:
        """The range's candles from the first trade minute on, a month at a time, each month's funding
        rows added to the pending ones as the month is read; ``on_month`` after each month."""
        for month in self.months():
            started = time.monotonic()
            events_before = self.events
            self._pending += [r for r in self._tree.read_funding(month) if self._start < r.charge_ns < self._end]
            for candle in self._tree.read_bars(month):
                if candle.open_ns < self._start or candle.ts_ns > self._end:
                    continue
                if self.first_trade_ns is None:
                    if candle.quiet:
                        continue
                    self.first_trade_ns = candle.open_ns
                yield candle
            if self._on_month is not None:
                self._on_month(month, self.events - events_before, time.monotonic() - started)

    def start_merge(self) -> None:
        """Ready this coin for ``merged_chunks``: read up to its first candle (``chunks`` is not used)."""
        self._cursor = self._candles()
        self._next = next(self._cursor, None)

    def _point_due_ns(self) -> int | None:
        # The next minute's first point (a quiet minute has only its close point), else the final quote.
        if self._next is not None:
            return self._next.open_ns + POINT_OFFSETS_NS[-1 if self._next.quiet else 0]
        if not self._final_sent and self.last_point is not None:
            return final_point(self.last_point).ts_ns
        return None

    def due_ns(self) -> int | None:
        """When this coin's next event is: a pending funding row, its next minute's first point, or its
        final quote once its candles have run out; None when it has sent everything."""
        pending = self._pending[0].charge_ns if self._pending else None
        times = [t for t in (pending, self._point_due_ns()) if t is not None]
        return min(times) if times else None

    def take(self, step_ns: int) -> list[Data]:
        """This coin's events due at ``step_ns``: its pending funding rows charged by then, and its next
        minute if that starts then (its path built now, from the side flag as it is) or its final quote."""
        events: list[Data] = []
        while self._pending and self._pending[0].charge_ns <= step_ns:
            events.append(self._funding(self._pending.pop(0)))
        if self._point_due_ns() == step_ns:
            if self._next is not None:
                points = path_points(self._next, self._flag.side, self._slippage, self._tick, self._step)
                for point in points:
                    events.extend(self._point(point))
                self.last_point = points[-1]
                self._next = next(self._cursor, None)
            else:
                assert self.last_point is not None  # _point_due_ns gave the final quote's time
                events.extend(self._point(final_point(self.last_point)))
                self._final_sent = True
                self._pending.clear()  # rows after the final quote never reach the strategy, as in ``chunks``
        self.events += len(events)
        return events

    def _funding(self, row: FundingRow) -> FundingRateUpdate:
        return FundingRateUpdate(
            self._instrument_id, row.rate, row.charge_ns, row.charge_ns, next_funding_ns=row.next_funding_ns
        )

    def _point(self, point: PathPoint) -> list[Data]:
        events: list[Data] = []
        if point.trade_size is not None:
            events.append(
                TradeTick(
                    self._instrument_id,
                    Price(point.price, self._price_precision),
                    Quantity(point.trade_size, self._size_precision),
                    AggressorSide.NO_AGGRESSOR,
                    TradeId(point.trade_id),
                    point.ts_ns,
                    point.ts_ns,
                )
            )
        events.append(
            QuoteTick(
                self._instrument_id,
                Price(point.bid, self._price_precision),
                Price(point.ask, self._price_precision),
                self._quote_size,
                self._quote_size,
                point.ts_ns,
                point.ts_ns,
            )
        )
        return events


def merged_chunks(streams: Sequence[CandleStream]) -> Iterator[list[Data]]:
    """Several coins' streams as one data iterator (spec 0022 AC-7): one chunk per due time, the earliest
    over the coins, holding every coin's events due then, ordered by ``ts_init`` and then by the
    streams' order (sorted instrument id), so equal timestamps never depend on Nautilus's tie breaking.

    A coin's minute is built only when it is due, so its path reads the side flag after every earlier
    event has been processed, as in a single coin's ``chunks``.
    """
    for stream in streams:
        stream.start_merge()
    while True:
        due = [t for t in (s.due_ns() for s in streams) if t is not None]
        if not due:
            return
        step = min(due)
        tagged = [(event.ts_init, n, i, event) for n, s in enumerate(streams) for i, event in enumerate(s.take(step))]
        yield [event for *_, event in sorted(tagged, key=lambda t: t[:3])]
