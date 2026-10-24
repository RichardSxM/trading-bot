"""The data audit's math (spec 0017 AC-2): spreads, moves, and order flow of the free Tardis days.

The caller feeds one clean segment at a time, its quotes and trades in stream order, as plain
values (no Nautilus types). At each whole UTC second inside the segment the last quote at or
before it gives one sample of the quoted spread in bps and the touch depth; the mid moves over 1,
5, and 15 minutes pair two such seconds of one segment. Trades fold into 1 minute buckets by
``ts_event`` (zero trade minutes count). Percentiles use the nearest rank method.

Samples are kept as integers in units of ``SCALE`` (bps or BTC to 4 places, rounded half even), so
a year of seconds fits in memory and two runs give the same numbers.
"""

from array import array
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from trading_core.marketdata.hours import HOUR_NS

SECOND_NS: Final = 1_000_000_000
MINUTE_NS: Final = 60 * SECOND_NS
BPS: Final = Decimal(10_000)
SCALE: Final = Decimal("0.0001")
HORIZONS_MIN: Final = (1, 5, 15)
MOVE_LEVELS_BPS: Final = (Decimal("10.5"), Decimal(15))
SPREAD_PERCENTILES: Final = (50, 90, 95, 99)
# The pinned PF_XBTUSD fees (spec 0008 AC-11).
MAKER_FEE: Final = Decimal("0.0002")
TAKER_FEE: Final = Decimal("0.0005")
ENTRY_COSTS_BPS: Final = {
    "maker_entry": (MAKER_FEE + TAKER_FEE) * BPS,
    "taker_entry": 2 * TAKER_FEE * BPS,
}

type Aggressor = Literal["buy", "sell", "none"]


def scaled(value: Decimal) -> int:
    """``value`` in units of ``SCALE``, rounded half even."""
    return int((value / SCALE).to_integral_value(ROUND_HALF_EVEN))


def unscaled(units: int) -> Decimal:
    """The ``Decimal`` a ``scaled`` integer stands for."""
    return Decimal(units) * SCALE


def nearest_rank(values: Sequence[int], percentile: int) -> int | None:
    """The nearest rank percentile of ``values`` (sorted here), or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = -(-percentile * len(ordered) // 100)  # ceil(p / 100 * n)
    return ordered[max(rank, 1) - 1]


@dataclass
class Samples:
    """One group's samples (a day, a UTC hour of the day, or everything pooled), all ``scaled``."""

    spread_bps: array[int] = field(default_factory=lambda: array("q"))
    depth_btc: array[int] = field(default_factory=lambda: array("q"))
    moves_bps: dict[int, array[int]] = field(default_factory=lambda: {h: array("q") for h in HORIZONS_MIN})
    trades_per_min: array[int] = field(default_factory=lambda: array("q"))
    abs_delta_btc: array[int] = field(default_factory=lambda: array("q"))
    buy_size: int = 0
    traded_size: int = 0

    def extend(self, other: Samples) -> None:
        """Add another group's samples to this one."""
        self.spread_bps.extend(other.spread_bps)
        self.depth_btc.extend(other.depth_btc)
        for horizon in HORIZONS_MIN:
            self.moves_bps[horizon].extend(other.moves_bps[horizon])
        self.trades_per_min.extend(other.trades_per_min)
        self.abs_delta_btc.extend(other.abs_delta_btc)
        self.buy_size += other.buy_size
        self.traded_size += other.traded_size


@dataclass(frozen=True)
class _Quote:
    bid: Decimal
    ask: Decimal
    depth: Decimal


class SegmentAudit:
    """Samples of one clean segment ``[start_ns, end_ns]``, fed in stream order, then ``close``\\ d.

    ``samples`` holds the segment's samples; ``by_hour`` holds the spread and 5 minute move
    samples per UTC hour of the day (0 to 23) of the second they start at.
    """

    def __init__(self, start_ns: int, end_ns: int) -> None:
        if start_ns % MINUTE_NS or (end_ns + 1) % MINUTE_NS or end_ns < start_ns:
            raise ValueError("a segment covers whole minutes")
        self._start = start_ns
        self._end = end_ns
        self._next_second = start_ns
        self._last: _Quote | None = None
        # The mid at each whole second, by its offset from the start (None before the first quote).
        self._mids: list[Decimal | None] = []
        self._minutes = (end_ns + 1 - start_ns) // MINUTE_NS
        self._trades = [0] * self._minutes
        self._delta = [Decimal(0)] * self._minutes
        self.samples = Samples()
        self.by_hour: dict[int, Samples] = {}
        self.late_trades = 0
        self._closed = False

    def on_quote(self, ts_ns: int, bid: Decimal, ask: Decimal, bid_size: Decimal, ask_size: Decimal) -> None:
        """A top of book change at ``ts_ns``; a crossed or empty top is ignored."""
        self._sample_until(ts_ns)
        if bid <= 0 or ask <= bid:
            return
        self._last = _Quote(bid, ask, bid_size + ask_size)

    def on_trade(self, ts_ns: int, size: Decimal, aggressor: Aggressor) -> None:
        """A trade, bucketed by its ``ts_event`` minute; outside the segment it counts in ``late_trades``."""
        if not self._start <= ts_ns <= self._end:
            self.late_trades += 1
            return
        minute = (ts_ns - self._start) // MINUTE_NS
        self._trades[minute] += 1
        units = scaled(size)
        self.samples.traded_size += units
        if aggressor == "buy":
            self._delta[minute] += size
            self.samples.buy_size += units
        elif aggressor == "sell":
            self._delta[minute] -= size

    def close(self) -> None:
        """Sample the seconds left up to the segment end, then the moves and the minute buckets."""
        if self._closed:
            raise ValueError("segment already closed")
        self._closed = True
        self._sample_until(self._end + 1)
        self._fold_moves()
        self.samples.trades_per_min.extend(self._trades)
        self.samples.abs_delta_btc.extend(scaled(abs(d)) for d in self._delta)

    def _hour(self, second_ns: int) -> Samples:
        return self.by_hour.setdefault((second_ns // HOUR_NS) % 24, Samples())

    def _sample_until(self, ts_ns: int) -> None:
        # Every whole second before ts_ns takes the quote that was last before ts_ns.
        while self._next_second < ts_ns and self._next_second <= self._end:
            quote = self._last
            if quote is None:
                self._mids.append(None)
            else:
                mid = (quote.bid + quote.ask) / 2
                spread = scaled((quote.ask - quote.bid) / mid * BPS)
                self._mids.append(mid)
                self.samples.spread_bps.append(spread)
                self.samples.depth_btc.append(scaled(quote.depth))
                self._hour(self._next_second).spread_bps.append(spread)
            self._next_second += SECOND_NS

    def _fold_moves(self) -> None:
        for horizon in HORIZONS_MIN:
            step = horizon * 60
            for offset in range(len(self._mids) - step):
                start, later = self._mids[offset], self._mids[offset + step]
                if start is None or later is None:
                    continue
                move = scaled(abs(later - start) / start * BPS)
                self.samples.moves_bps[horizon].append(move)
                if horizon == 5:
                    self._hour(self._start + offset * SECOND_NS).moves_bps[5].append(move)


@dataclass(frozen=True)
class Span:
    """A clean segment's time range, ``[start_ns, end_ns]``."""

    start_ns: int
    end_ns: int


@dataclass(frozen=True)
class Rate:
    """One ``funding_rate_update``: when it was seen, and the rate."""

    ts_ns: int
    rate: Decimal


def mean_abs_funding(spans: Iterable[Span], rates: Sequence[Rate]) -> Decimal | None:
    """The time weighted mean absolute funding rate over the spans, or None when no rate covers any of them.

    Inside a span the rate in force is the last one at or before that moment; a span with no rate
    before it counts from its first rate.
    """
    ordered = sorted(rates, key=lambda r: r.ts_ns)
    times = [r.ts_ns for r in ordered]
    weighted = Decimal(0)
    covered = 0
    for span in spans:
        end = span.end_ns + 1
        index = bisect_right(times, span.start_ns) - 1
        if index < 0:
            index = 0
            if not ordered or ordered[0].ts_ns >= end:
                continue
            moment = ordered[0].ts_ns
        else:
            moment = span.start_ns
        while moment < end:
            following = ordered[index + 1].ts_ns if index + 1 < len(ordered) else end
            until = min(following, end)
            weighted += abs(ordered[index].rate) * (until - moment)
            covered += until - moment
            moment = until
            index += 1
    if covered == 0:
        return None
    return weighted / covered


def _figure(units: int | None) -> str | None:
    return None if units is None else str(unscaled(units))


def _share_at_least(values: Sequence[int], level: Decimal) -> str | None:
    if not values:
        return None
    floor = scaled(level)
    return str((Decimal(sum(1 for v in values if v >= floor)) / len(values)).quantize(SCALE, ROUND_HALF_EVEN))


def summary(samples: Samples) -> dict[str, object]:
    """The AC-2 figures of one group, as JSON ready text (None where there is no sample)."""
    moves: dict[str, object] = {}
    for horizon in HORIZONS_MIN:
        values = samples.moves_bps[horizon]
        moves[f"{horizon}m"] = {
            "median_abs_bps": _figure(nearest_rank(values, 50)),
            **{f"share_at_least_{level}_bps": _share_at_least(values, level) for level in MOVE_LEVELS_BPS},
        }
    buy_share = (
        None
        if samples.traded_size == 0
        else str((Decimal(samples.buy_size) / samples.traded_size).quantize(SCALE, ROUND_HALF_EVEN))
    )
    return {
        "spread_bps": {f"p{p}": _figure(nearest_rank(samples.spread_bps, p)) for p in SPREAD_PERCENTILES},
        "touch_depth_btc_p50": _figure(nearest_rank(samples.depth_btc, 50)),
        "mid_moves": moves,
        "trades_per_minute_p50": nearest_rank(samples.trades_per_min, 50),
        "aggressive_buy_share": buy_share,
        "abs_delta_btc_per_minute": {
            "p50": _figure(nearest_rank(samples.abs_delta_btc, 50)),
            "p90": _figure(nearest_rank(samples.abs_delta_btc, 90)),
        },
        "seconds_sampled": len(samples.spread_bps),
    }


def hour_summary(samples: Samples) -> dict[str, object]:
    """One UTC hour of the day: spread p50 and the median absolute 5 minute move, in bps."""
    return {
        "spread_bps_p50": _figure(nearest_rank(samples.spread_bps, 50)),
        "median_abs_5m_move_bps": _figure(nearest_rank(samples.moves_bps[5], 50)),
    }


def reachable(pooled: Samples, fee_hurdle_k: Decimal) -> dict[str, bool | None]:
    """Per entry cost, whether the pooled median absolute 5 minute move is at least ``fee_hurdle_k`` times it."""
    median = nearest_rank(pooled.moves_bps[5], 50)
    return {
        name: None if median is None else unscaled(median) >= fee_hurdle_k * cost
        for name, cost in ENTRY_COSTS_BPS.items()
    }


@dataclass(frozen=True)
class DayAudit:
    """One day's samples and plan facts, as the command gathers them."""

    day: date
    samples: Samples
    by_hour: Mapping[int, Samples]
    clean_hours: int
    dropped_hours: Mapping[str, int]
    mean_abs_funding: Decimal | None
    late_trades: int


def audit_json(days: Sequence[DayAudit], fee_hurdle_k: Decimal, generated_at: str) -> dict[str, object]:
    """The whole ``audit.json``: pooled, per day, and per UTC hour figures plus ``reachable`` (AC-2)."""
    pooled = Samples()
    hours: dict[int, Samples] = {}
    for day in days:
        pooled.extend(day.samples)
        for hour, samples in day.by_hour.items():
            hours.setdefault(hour, Samples()).extend(samples)
    return {
        "generated_at": generated_at,
        "days": [d.day.isoformat() for d in days],
        "fee_hurdle_k": str(fee_hurdle_k),
        "entry_costs_bps": {name: str(cost) for name, cost in ENTRY_COSTS_BPS.items()},
        "pooled": summary(pooled),
        "reachable": reachable(pooled, fee_hurdle_k),
        "per_hour": {f"{h:02d}": hour_summary(hours[h]) for h in sorted(hours)},
        "per_day": {
            d.day.isoformat(): {
                **summary(d.samples),
                "clean_hours": d.clean_hours,
                "dropped_hours": dict(sorted(d.dropped_hours.items())),
                "mean_abs_funding_rate": None if d.mean_abs_funding is None else str(d.mean_abs_funding),
                "late_trades": d.late_trades,
            }
            for d in days
        },
    }
