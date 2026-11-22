"""A book strategy as one candle unit (spec 0033 AC-19): what ``lab study`` and ``lab holdout`` run for ``trend_vol``.

A candle batch of a book strategy (``lab.strategies.BOOK_REGISTRY``) runs each variant as one unit with one
strategy instance over every coin, as the book mode (``lab.book_mode``, proven in parity by AC-10a) builds it:
the trader's settings under the run's ``[strategy.config]`` and grid point, a pause gate (loaded, never paused),
the daily close ``seeds`` (each coin's 00:00 closes before the range, ``seed_days`` of them) and ``last_start_ns``
None, ``BOOK_QUOTE_SIZE`` quotes with the quiet lead, and the engine started at the first day's 00:03 UTC. On top
of the book mode, as every candle unit has: the run's maker and taker fees and slippage, its risk limits, the
``LabRecorder`` sampling funding at every charge time, and one range end closer per coin, so the unit ends flat.

Funding (AC-19c): one rate table per coin (``resolved_rates``: the stored rate the candle lab carries, at most
3 h old) books into the engine account through ``FundingModule``, so the strategy sizes from a balance net of
funding, and the parent pays the bundle's funding from the same table (``book_payments``). A charge time with
no rate pays nothing, and every held coin there is counted.

A coin that lists inside the range (HYPE) streams from its first stored minute; the rule treats it as any coin
without enough history until it has ``min_history_days`` closes. A coin with no candle in the whole range fails
the unit. No liquidation guard runs: the book's gross cap keeps it far from the venue's margin.
"""

import logging
import resource
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from nautilus_trader.model.identifiers import InstrumentId

from lab.actors import RANGE_END, LabRecorder, SegmentEndCloser, UnitRows
from lab.book_mode import BOOK_QUOTE_SIZE, BTC, DAY_NS, START_OFFSET_NS, FundingModule, daily_seed
from lab.candle_stream import CandleStream, PositionSideFlag, SideFlag, merged_chunks
from lab.candle_units import CLOSE_DELAY_NS, CandleUnitPlan, CandleUnitResult, CoinUnitFacts, make_candle_engine
from lab.instruments import pinned_instrument
from lab.strategies import RunContext, book_entry
from lab.units import UNIT_RUN_ID, Sequenced
from trading_core.catalog.candles import CandleTree
from trading_core.lab.book_funding import resolved_rates
from trading_core.lab.candle_path import FINAL_QUOTE_DELAY_NS
from trading_core.lab.candles import FUNDING_CARRY_MAX_NS, Month, expected_charge_times
from trading_core.lab.errors import RecordingFailedError
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose

log = logging.getLogger(__name__)

# Months read for a seed: the seed's days plus a margin for a month boundary and missing closes.
SEED_MARGIN_DAYS: Final = 3


def _day(ns: int) -> date:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=UTC).date()


def seed_closes(tree: CandleTree, first: date, seed_days: int) -> tuple[DailyClose, ...]:
    """A coin's daily closes before ``first`` (``daily_seed``), from the months that can hold them; none when the
    coin has no candle yet."""
    start = first - timedelta(days=seed_days + SEED_MARGIN_DAYS)
    month = Month(start.year, start.month)
    last = Month(first.year, first.month)
    hourly: dict[int, Decimal] = {}
    while month <= last:
        if tree.bar_file(month).is_file():
            hourly.update({c.ts_ns: c.close for c in tree.read_bars(month) if c.ts_ns % DAY_NS == 0})
        month = month.next()
    return daily_seed(hourly, first, seed_days)


def coin_rates(tree: CandleTree, times: list[int]) -> dict[int, Decimal]:
    """The coin's rate at each charge time (``resolved_rates`` over its stored rows)."""
    if not times:
        return {}
    first, last = _day(times[0] - FUNDING_CARRY_MAX_NS), _day(times[-1])
    month, end = Month(first.year, first.month), Month(last.year, last.month)
    rows = []
    while month <= end:
        rows += tree.read_funding(month)
        month = month.next()
    return resolved_rates(sorted(rows, key=lambda r: r.charge_ns), times)


def run_book_unit(plan: CandleUnitPlan) -> CandleUnitResult:
    """Run one variant of a book strategy over the whole range (module docstring).

    Raises ``RecordingFailedError`` when a coin has no candle in the range, an event could not be recorded, or a
    position is still open after the range end close; any of these fails the variant (spec 0014 AC-18).
    """
    started = time.monotonic()
    if plan.start_ns % DAY_NS:
        raise ValueError(f"a book unit starts at 00:00 UTC, not at {plan.start_ns}")
    book = book_entry(plan.strategy_name)
    coins = tuple(sorted(plan.instrument_ids))
    rows = UnitRows()
    gate = PauseGate()
    gate.unpause()  # loaded, not paused: the lab never pauses
    blocks = EntryBlocks()
    context = RunContext(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=BTC if BTC in coins else coins[0],
        seed=plan.seed,
        risk=plan.risk,
        order_id_tag=book.order_id_tag,
        taker_fee=plan.taker_fee,
        stale_quote_s=plan.stale_quote_s,
        lab="candle",
        instruments=coins,
    )
    config = book.entry.build_config({**book.config, **plan.strategy_config}, context)
    seed_days = getattr(config, "seed_days", None)  # every book config has it; the type is the base config
    if not isinstance(seed_days, int):
        raise RecordingFailedError(f"book config {type(config).__name__} has no seed_days")
    first = _day(plan.start_ns)
    trees = {coin: CandleTree(plan.candles_root, pinned_instrument(coin)) for coin in coins}
    seeds = {coin: seed_closes(tree, first, seed_days) for coin, tree in trees.items()}
    strategy = book.build(config, gate, blocks, rows, seeds, None)
    stream_start = plan.start_ns + START_OFFSET_NS
    ids = [InstrumentId.from_str(coin) for coin in coins]
    flags = {i: SideFlag() for i in ids}
    streams: dict[InstrumentId, CandleStream] = {}
    for i in ids:
        stream = CandleStream(
            trees[i.value],
            stream_start,
            plan.end_ns,
            plan.slippage_bps,
            flags[i],
            quiet_lead=True,
            quote_size=BOOK_QUOTE_SIZE,
        )
        if stream.last_point_ns() is None:
            raise RecordingFailedError(f"variant {plan.variant}: the range holds no {i.value} candle")
        streams[i] = stream
    last_points = {i: s.last_point_ns() or 0 for i, s in streams.items()}
    closers = [
        SegmentEndCloser(
            strategy=strategy,
            gate=gate,
            rows=rows,
            env=plan.env,
            run_id=UNIT_RUN_ID,
            instrument_id=i,
            close_at_ns=last_points[i] + CLOSE_DELAY_NS,
            reason=RANGE_END,
            component_id=f"SegmentEndCloser-{n}",
            close_late_opens=True,
        )
        for n, i in enumerate(ids, start=1)
    ]
    charge_times = expected_charge_times(stream_start, plan.end_ns)
    rates = {coin: coin_rates(tree, charge_times) for coin, tree in trees.items()}
    funding = FundingModule(charge_times, rates, ids)
    recorder = LabRecorder(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_ids=ids,
        rows=rows,
        blocks=blocks,
        risk=plan.risk,
        funding_boundaries=charge_times,
    )
    engine = make_candle_engine(plan, modules=[funding])
    try:
        engine.add_actor(recorder)
        engine.add_actor(PositionSideFlag(flags))
        engine.add_strategy(strategy)
        for closer in closers:
            engine.add_actor(closer)
        engine.add_data_iterator("candles", merged_chunks(list(streams.values())))
        engine.run(start=stream_start, end=max(last_points.values()) + FINAL_QUOTE_DELAY_NS)
        if BlockReason.RECORDING_FAILED in blocks.active():
            raise RecordingFailedError(f"variant {plan.variant}: an event could not be recorded")
        if any(engine.cache.positions_open(instrument_id=i) for i in ids):
            raise RecordingFailedError(f"variant {plan.variant}: a position is open after the range end close")
    finally:
        engine.dispose()
    events = sum(s.events for s in streams.values())
    first_trades = [s.first_trade_ns for s in streams.values() if s.first_trade_ns is not None]
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    wall = time.monotonic() - started
    log.info(
        "lab_book_unit_done",
        extra={
            "fields": {
                "variant": plan.variant,
                "events": events,
                "funding_missing_samples": funding.missing,
                "wall_seconds": round(wall, 3),
                "ru_maxrss": max_rss,
            }
        },
    )
    return CandleUnitResult(
        variant=plan.variant,
        decisions=_sorted(rows.decisions.values()),
        risk_events=_sorted(rows.risk_events.values()),
        orders=_sorted(rows.orders.values()),
        order_events=_sorted(rows.order_events.values()),
        fills=_sorted(rows.fills.values()),
        positions=_sorted(rows.positions.values()),
        funding_samples={k: tuple(rows.funding_samples[k]) for k in sorted(rows.funding_samples)},
        per_instrument={
            i.value: CoinUnitFacts(
                zero_volume_bars=0,  # a book strategy subscribes to no bars
                first_trade_ns=s.first_trade_ns,
                last_point_ns=last_points[i],
                events=s.events,
            )
            for i, s in streams.items()
        },
        zero_volume_bars=0,
        first_trade_ns=min(first_trades, default=None),
        last_point_ns=max(last_points.values()),
        ruined_ns=None,
        events=events,
        month_events=(),
        month_seconds=(),
        wall_seconds=wall,
        max_rss_bytes=max_rss,
    )


def _sorted[R](rows: Iterable[tuple[int, R]]) -> Sequenced[R]:
    return tuple(sorted(rows, key=lambda pair: pair[0]))
