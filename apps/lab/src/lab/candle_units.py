"""One candle unit: one variant over a whole candle range in a fresh engine (spec 0014 AC-8, AC-11, AC-14).

``run_candle_unit`` builds the AC-8 venue (L1 book, the run's fees, no latency, no queue, fills only
from quotes), adds the pinned instrument with the run's fees, the ``LabRecorder`` (sampling funding
at every expected charge time and counting the strategy's zero volume bars), the
``PositionSideFlag``, the registered strategy built exactly as the trader builds it, the
``LiquidationGuard`` (which can end the run early on ruin), and the range end closer, then feeds the
candle stream through ``add_data_iterator``. It returns a picklable
``CandleUnitResult``. Rows carry the placeholder ``UNIT_RUN_ID``; the parent stamps the run id.

A multi coin plan (spec 0022) does the same for each coin inside the one engine and margin account,
and feeds the coins' streams as one ``merged_chunks`` iterator.
"""

import logging
import resource
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.common.component import set_backtest_force_stop
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money

from lab.actors import RANGE_END, LabRecorder, LiquidationGuard, RuinFlag, SegmentEndCloser, SharedRuin, UnitRows
from lab.candle_stream import CandleStream, MonthDone, PositionSideFlag, SideFlag, merged_chunks
from lab.instruments import pinned_instrument, with_fees
from lab.strategies import ConfigValue, MarketContextValues, RunContext, registry_entry
from lab.stream import rss_bytes
from lab.units import NS_PER_MS, TRADER_ID, UNIT_RUN_ID, Sequenced
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candle_path import FINAL_QUOTE_DELAY_NS
from trading_core.lab.candles import CANDLE_STALE_CHECK_S, Month, expected_charge_times
from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import FundingSample
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import Env
from trading_core.recording.rows import DecisionRow, FillRow, OrderEventRow, OrderRow, PositionRow, RiskEventRow
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

# The range end closer acts this long after the last path point, before the final quote (AC-14).
CLOSE_DELAY_NS: Final = NS_PER_MS


@dataclass(frozen=True)
class CandleUnitPlan:
    """Everything one candle unit needs, picklable so a spawned worker can take it."""

    variant: str
    candles_root: Path
    # The run's coins in sorted order; one for a single coin run (spec 0022 AC-4).
    instrument_ids: tuple[str, ...]
    start_ns: int
    end_ns: int
    maker_fee: Decimal
    taker_fee: Decimal
    slippage_bps: Decimal
    starting_balance_usd: Decimal
    leverage: Decimal
    risk: RiskLimits
    seed: int
    strategy_name: str
    strategy_config: Mapping[str, ConfigValue]
    stale_quote_s: Decimal
    env: Env = Env.DEMO
    test_strategies: bool = False
    market: MarketContextValues = field(default_factory=MarketContextValues)
    # A look ahead cut run (AC-19): the same data ending at this cut, a minute off the hour (``end_ns`` equals it).
    cut_ns: int | None = None

    @property
    def label(self) -> str:
        """One unit runs the whole range (or up to a look ahead cut), for the worker pool's process names and logs."""
        return "candles" if self.cut_ns is None else f"cut-{self.cut_ns}"


@dataclass(frozen=True)
class CandleUnitResult:
    """A candle unit's rows with their sequence inside the unit, the funding samples, and what the report needs.

    ``wall_seconds``, ``max_rss_bytes``, and ``month_seconds`` are for logs and verify.md only: they
    differ run to run, so no report or row carries them (AC-17).
    """

    variant: str
    decisions: Sequenced[DecisionRow]
    risk_events: Sequenced[RiskEventRow]
    orders: Sequenced[OrderRow]
    order_events: Sequenced[OrderEventRow]
    fills: Sequenced[FillRow]
    positions: Sequenced[PositionRow]
    # Each coin's samples by instrument id, keys in sorted order; one key for a single coin run (spec 0022 AC-8).
    funding_samples: Mapping[str, tuple[FundingSample, ...]]
    zero_volume_bars: int
    first_trade_ns: int | None
    last_point_ns: int
    # When account equity reached zero and the variant ended early (AC-14), else None.
    ruined_ns: int | None
    events: int
    # (month, events) per month streamed, and that month's wall seconds.
    month_events: tuple[tuple[str, int], ...]
    month_seconds: tuple[float, ...]
    wall_seconds: float
    max_rss_bytes: int


def _sorted[R](rows: Iterable[tuple[int, R]]) -> Sequenced[R]:
    return tuple(sorted(rows, key=lambda pair: pair[0]))


def make_candle_engine(plan: CandleUnitPlan) -> BacktestEngine:
    """The AC-8 venue: fills come only from the synthetic quotes, at the run's fees, with no latency; one
    margin account for every coin of the plan (spec 0022 AC-4)."""
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId(TRADER_ID), logging=LoggingConfig(log_level="ERROR"))
    )
    engine.add_venue(
        venue=Venue(InstrumentId.from_str(plan.instrument_ids[0]).venue.value),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(plan.starting_balance_usd, USD)],
        base_currency=USD,
        default_leverage=plan.leverage,
        fee_model=MakerTakerFeeModel(),
        book_type=BookType.L1_MBP,
        queue_position=False,
        liquidity_consumption=False,
        trade_execution=False,
        bar_execution=False,
        use_reduce_only=True,
    )
    for instrument_id in plan.instrument_ids:
        engine.add_instrument(with_fees(instrument_id, plan.maker_fee, plan.taker_fee))
    return engine


@dataclass(frozen=True)
class _Coin:
    """One coin of a unit and everything it owns alone (spec 0022 AC-4)."""

    instrument_id: InstrumentId
    strategy: KfsStrategy
    blocks: EntryBlocks
    bar_type: BarType
    flag: SideFlag
    stream: CandleStream
    last_point_ns: int
    closer: SegmentEndCloser
    guard: LiquidationGuard


def run_candle_unit(plan: CandleUnitPlan) -> CandleUnitResult:
    """Run one variant over the whole range and return its rows.

    A multi coin plan runs every coin in this one engine and margin account: per coin a strategy
    instance (``order_id_tag`` ``C1``, ``C2``, … in sorted id order), pause gate, entry blocks, stream,
    range end closer, and liquidation guard; shared the rows, the recorder, the side flag actor, and
    the ruin stop (spec 0022 AC-4, AC-4b). Raises ``RecordingFailedError`` when a coin has no candle in
    the range, an event could not be recorded, or a position is still open after the range end close;
    any of these fails the variant (AC-18).
    """
    started = time.monotonic()
    fields: dict[str, object] = {"variant": plan.variant}
    multi = len(plan.instrument_ids) > 1
    rows = UnitRows()
    entry = registry_entry(plan.strategy_name, test_strategies=plan.test_strategies)
    ruin = RuinFlag()
    # The engine's own early stop: its loop ends at the next event, with no timer flushed to the
    # range end, so nothing reaches the strategy after ruin (AC-14).
    shared_ruin = SharedRuin(ruin, stop=lambda: set_backtest_force_stop(True))
    month_events: list[tuple[str, int]] = []
    month_seconds: list[float] = []

    def month_done(coin: str) -> MonthDone:
        def done(month: Month, events: int, seconds: float) -> None:
            month_events.append((f"{coin} {month}" if multi else str(month), events))
            month_seconds.append(seconds)
            log.info(
                "lab_candle_month_replayed",
                extra={
                    "fields": {
                        **fields,
                        **({"instrument_id": coin} if multi else {}),
                        "month": str(month),
                        "events": events,
                        "wall_seconds": round(seconds, 3),
                        "events_per_s": round(events / seconds) if seconds else None,
                        "rss_bytes": rss_bytes(),
                    }
                },
            )

        return done

    coins: list[_Coin] = []
    for number, coin in enumerate(plan.instrument_ids, start=1):
        tag = f"C{number}"
        instrument_id = InstrumentId.from_str(coin)
        gate = PauseGate()
        gate.unpause()  # loaded, not paused: the lab never pauses
        blocks = EntryBlocks()
        context = RunContext(
            env=plan.env,
            run_id=UNIT_RUN_ID,
            instrument_id=coin,
            seed=plan.seed,
            risk=plan.risk,
            order_id_tag=tag,
            taker_fee=plan.taker_fee,
            market=plan.market,
            stale_quote_s=plan.stale_quote_s,
            stale_check_s=CANDLE_STALE_CHECK_S,
            lab="candle",
        )
        config = entry.build_config(plan.strategy_config, context)
        strategy = entry.build(config, gate, blocks, rows)
        flag = SideFlag()
        tree = CandleTree(plan.candles_root, pinned_instrument(coin))
        stream = CandleStream(tree, plan.start_ns, plan.end_ns, plan.slippage_bps, flag, on_month=month_done(coin))
        last_point = stream.last_point_ns()
        if last_point is None:
            raise RecordingFailedError(
                f"variant {plan.variant}: the range holds no {coin + ' ' if multi else ''}candle"
            )
        coins.append(
            _Coin(
                instrument_id=instrument_id,
                strategy=strategy,
                blocks=blocks,
                bar_type=BarType.from_str(f"{coin}-{getattr(config, 'bar_spec', None)}-INTERNAL"),
                flag=flag,
                stream=stream,
                last_point_ns=last_point,
                closer=SegmentEndCloser(
                    strategy=strategy,
                    gate=gate,
                    rows=rows,
                    env=plan.env,
                    run_id=UNIT_RUN_ID,
                    instrument_id=instrument_id,
                    close_at_ns=last_point + CLOSE_DELAY_NS,
                    reason=RANGE_END,
                    component_id=f"SegmentEndCloser-{tag}" if multi else None,
                ),
                guard=LiquidationGuard(
                    strategy=strategy,
                    rows=rows,
                    env=plan.env,
                    run_id=UNIT_RUN_ID,
                    instrument_id=instrument_id,
                    taker_fee=plan.taker_fee,
                    risk=plan.risk,
                    ruin=ruin,
                    on_ruin=shared_ruin,
                    component_id=f"LiquidationGuard-{tag}" if multi else None,
                ),
            )
        )
        shared_ruin.guards.append(coins[-1].guard)
    # The engine runs to the latest final quote over the coins (AC-4).
    final_ts = max(c.last_point_ns for c in coins) + FINAL_QUOTE_DELAY_NS
    recorder = LabRecorder(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_ids=[c.instrument_id for c in coins],
        rows=rows,
        # A recording failure fails the variant whichever blocks hold it (checked below for every coin).
        blocks=coins[0].blocks,
        risk=plan.risk,
        funding_boundaries=expected_charge_times(plan.start_ns, plan.end_ns),
        bar_types=[c.bar_type for c in coins],
    )
    for c in coins:
        # The funding row known at the range start, so the first entry is not refused as funding_stale (AC-6).
        c.strategy.warm_market_context(c.stream.warmup())
    engine = make_candle_engine(plan)
    try:
        engine.add_actor(recorder)
        engine.add_actor(PositionSideFlag({c.instrument_id: c.flag for c in coins}))
        for c in coins:
            engine.add_strategy(c.strategy)
        for c in coins:
            engine.add_actor(c.guard)
        for c in coins:
            engine.add_actor(c.closer)
        streams = [c.stream for c in coins]
        engine.add_data_iterator("candles", merged_chunks(streams) if multi else streams[0].chunks())
        engine.run(start=plan.start_ns, end=final_ts)
        if any(BlockReason.RECORDING_FAILED in c.blocks.active() for c in coins):
            raise RecordingFailedError(f"variant {plan.variant}: an event could not be recorded")
        if any(engine.cache.positions_open(instrument_id=c.instrument_id) for c in coins):
            raise RecordingFailedError(f"variant {plan.variant}: a position is open after the range end close")
    finally:
        engine.dispose()

    events = sum(c.stream.events for c in coins)
    first_trades = [c.stream.first_trade_ns for c in coins if c.stream.first_trade_ns is not None]
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    wall = time.monotonic() - started
    log.info(
        "lab_unit_done",
        extra={"fields": {**fields, "events": events, "wall_seconds": round(wall, 3), "ru_maxrss": max_rss}},
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
        zero_volume_bars=recorder.zero_volume_bars,
        first_trade_ns=min(first_trades, default=None),
        last_point_ns=max(c.last_point_ns for c in coins),
        ruined_ns=ruin.at_ns,
        events=events,
        month_events=tuple(month_events),
        month_seconds=tuple(month_seconds),
        wall_seconds=wall,
        max_rss_bytes=max_rss,
    )
