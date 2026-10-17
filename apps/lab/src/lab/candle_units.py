"""One candle unit: one variant over a whole candle range in a fresh engine (spec 0014 AC-8, AC-11, AC-14).

``run_candle_unit`` builds the AC-8 venue (L1 book, the run's fees, no latency, no queue, fills only
from quotes), adds the pinned instrument with the run's fees, the ``LabRecorder`` (sampling funding
at every expected charge time and counting the strategy's zero volume bars), the
``PositionSideFlag``, the registered strategy built exactly as the trader builds it, and the range
end closer, then feeds the candle stream through ``add_data_iterator``. It returns a picklable
``CandleUnitResult``. Rows carry the placeholder ``UNIT_RUN_ID``; the parent stamps the run id.
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
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money

from lab.actors import RANGE_END, LabRecorder, SegmentEndCloser, UnitRows
from lab.candle_stream import CandleStream, PositionSideFlag, SideFlag
from lab.instruments import pinned_instrument, with_fees
from lab.strategies import ConfigValue, MarketContextValues, RunContext, registry_entry
from lab.stream import rss_bytes
from lab.units import NS_PER_MS, TRADER_ID, UNIT_RUN_ID, Sequenced
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candle_path import FINAL_QUOTE_DELAY_NS
from trading_core.lab.candles import CANDLE_STALE_CHECK_S, Month, expected_charge_times
from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import FundingSample
from trading_core.node.strategy import PauseGate
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
    instrument_id: str
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

    @property
    def label(self) -> str:
        """One unit runs the whole range, for the worker pool's process names and logs."""
        return "candles"


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
    funding_samples: tuple[FundingSample, ...]
    zero_volume_bars: int
    first_trade_ns: int | None
    last_point_ns: int
    events: int
    # (month, events) per month streamed, and that month's wall seconds.
    month_events: tuple[tuple[str, int], ...]
    month_seconds: tuple[float, ...]
    wall_seconds: float
    max_rss_bytes: int


def _sorted[R](rows: Iterable[tuple[int, R]]) -> Sequenced[R]:
    return tuple(sorted(rows, key=lambda pair: pair[0]))


def make_candle_engine(plan: CandleUnitPlan) -> BacktestEngine:
    """The AC-8 venue: fills come only from the synthetic quotes, at the run's fees, with no latency."""
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId(TRADER_ID), logging=LoggingConfig(log_level="ERROR"))
    )
    engine.add_venue(
        venue=Venue(InstrumentId.from_str(plan.instrument_id).venue.value),
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
    engine.add_instrument(with_fees(plan.instrument_id, plan.maker_fee, plan.taker_fee))
    return engine


def run_candle_unit(plan: CandleUnitPlan) -> CandleUnitResult:
    """Run one variant over the whole range and return its rows.

    Raises ``RecordingFailedError`` when the range has no candle, an event could not be recorded,
    or a position is still open after the range end close; any of these fails the variant (AC-18).
    """
    started = time.monotonic()
    fields = {"variant": plan.variant}
    tree = CandleTree(plan.candles_root, pinned_instrument(plan.instrument_id))
    instrument_id = InstrumentId.from_str(plan.instrument_id)

    gate = PauseGate()
    gate.unpause()  # loaded, not paused: the lab never pauses
    blocks = EntryBlocks()
    rows = UnitRows()
    entry = registry_entry(plan.strategy_name, test_strategies=plan.test_strategies)
    context = RunContext(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=plan.instrument_id,
        seed=plan.seed,
        risk=plan.risk,
        order_id_tag="C1",
        taker_fee=plan.taker_fee,
        market=plan.market,
        stale_quote_s=plan.stale_quote_s,
        stale_check_s=CANDLE_STALE_CHECK_S,
    )
    config = entry.build_config(plan.strategy_config, context)
    strategy = entry.build(config, gate, blocks, rows)
    bar_type = BarType.from_str(f"{plan.instrument_id}-{getattr(config, 'bar_spec', None)}-INTERNAL")

    flag = SideFlag()
    month_events: list[tuple[str, int]] = []
    month_seconds: list[float] = []

    def month_done(month: Month, events: int, seconds: float) -> None:
        month_events.append((str(month), events))
        month_seconds.append(seconds)
        log.info(
            "lab_candle_month_replayed",
            extra={
                "fields": {
                    **fields,
                    "month": str(month),
                    "events": events,
                    "wall_seconds": round(seconds, 3),
                    "events_per_s": round(events / seconds) if seconds else None,
                    "rss_bytes": rss_bytes(),
                }
            },
        )

    stream = CandleStream(tree, plan.start_ns, plan.end_ns, plan.slippage_bps, flag, on_month=month_done)
    last_point = stream.last_point_ns()
    if last_point is None:
        raise RecordingFailedError(f"variant {plan.variant}: the range holds no candle")
    final_ts = last_point + FINAL_QUOTE_DELAY_NS
    closer = SegmentEndCloser(
        strategy=strategy,
        gate=gate,
        rows=rows,
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=instrument_id,
        close_at_ns=last_point + CLOSE_DELAY_NS,
        reason=RANGE_END,
    )
    recorder = LabRecorder(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=instrument_id,
        rows=rows,
        blocks=blocks,
        risk=plan.risk,
        funding_boundaries=expected_charge_times(plan.start_ns, plan.end_ns),
        bar_type=bar_type,
    )
    # The funding row known at the range start, so the first entry is not refused as funding_stale (AC-6).
    strategy.warm_market_context(stream.warmup())
    engine = make_candle_engine(plan)
    try:
        engine.add_actor(recorder)
        engine.add_actor(PositionSideFlag(flag))
        engine.add_strategy(strategy)
        engine.add_actor(closer)
        engine.add_data_iterator("candles", stream.chunks())
        engine.run(start=plan.start_ns, end=final_ts)
        if BlockReason.RECORDING_FAILED in blocks.active():
            raise RecordingFailedError(f"variant {plan.variant}: an event could not be recorded")
        if engine.cache.positions_open(instrument_id=instrument_id):
            raise RecordingFailedError(f"variant {plan.variant}: a position is open after the range end close")
    finally:
        engine.dispose()

    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    wall = time.monotonic() - started
    log.info(
        "lab_unit_done",
        extra={"fields": {**fields, "events": stream.events, "wall_seconds": round(wall, 3), "ru_maxrss": max_rss}},
    )
    return CandleUnitResult(
        variant=plan.variant,
        decisions=_sorted(rows.decisions.values()),
        risk_events=_sorted(rows.risk_events.values()),
        orders=_sorted(rows.orders.values()),
        order_events=_sorted(rows.order_events.values()),
        fills=_sorted(rows.fills.values()),
        positions=_sorted(rows.positions.values()),
        funding_samples=tuple(rows.funding_samples),
        zero_volume_bars=recorder.zero_volume_bars,
        first_trade_ns=stream.first_trade_ns,
        last_point_ns=last_point,
        events=stream.events,
        month_events=tuple(month_events),
        month_seconds=tuple(month_seconds),
        wall_seconds=wall,
        max_rss_bytes=max_rss,
    )
