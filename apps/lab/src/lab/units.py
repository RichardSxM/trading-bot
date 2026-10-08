"""One unit: one variant replayed over one segment in a fresh engine (spec 0008 AC-5, AC-7, AC-16).

``run_unit`` builds the AC-5 venue with the variant's fees, latency, and queue model, adds the
pinned instrument (fees replaced by the run's), the ``LabRecorder``, and the registered strategy
built as the trader builds it, feeds the hour stream through ``add_data_iterator``, and runs the
segment. It returns a picklable ``UnitResult``. Rows carry the placeholder ``UNIT_RUN_ID``; the
parent stamps the variant's run id after merging (AC-12).

Milestone 2 calls units in process. The worker pool (task 18) runs the same function in spawned
processes.
"""

import logging
import resource
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import LatencyModel, MakerTakerFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money

from lab.actors import LabRecorder, UnitRows
from lab.catalog import DELTAS_TABLE, SourceTree, last_ts_init
from lab.instruments import with_fees
from lab.strategies import ConfigValue, RunContext, registry_entry
from lab.stream import SegmentStream, rss_bytes
from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.segments import Segment
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env
from trading_core.recording.rows import DecisionRow, FillRow, OrderEventRow, OrderRow, PositionRow, RiskEventRow
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

TRADER_ID: Final = "BACKTESTER-LAB"
# Units build rows before the variant has a run id; the parent replaces this one (AC-12).
UNIT_RUN_ID: Final = UUID(int=0)
NS_PER_MS: Final = 1_000_000


@dataclass(frozen=True)
class UnitPlan:
    """Everything one unit needs, picklable so a spawned worker can take it."""

    variant: str
    segment: Segment
    source_root: Path
    instrument_id: str
    maker_fee: Decimal
    taker_fee: Decimal
    latency_ms: int
    queue_position: bool
    starting_balance_usd: Decimal
    leverage: Decimal
    risk: RiskLimits
    seed: int
    strategy_name: str
    strategy_config: Mapping[str, ConfigValue]
    env: Env = Env.DEMO


type Sequenced[R] = tuple[tuple[int, R], ...]


@dataclass(frozen=True)
class UnitResult:
    """A unit's rows with their sequence inside the unit, the spreads at order creation, and its cost.

    ``wall_seconds`` and ``max_rss_bytes`` are for logs only: they differ run to run, so no
    report or row carries them (AC-12).
    """

    variant: str
    segment: int
    decisions: Sequenced[DecisionRow]
    risk_events: Sequenced[RiskEventRow]
    orders: Sequenced[OrderRow]
    order_events: Sequenced[OrderEventRow]
    fills: Sequenced[FillRow]
    positions: Sequenced[PositionRow]
    spreads: Mapping[str, Decimal]
    events: int
    last_ts_init: int | None
    wall_seconds: float
    max_rss_bytes: int


def _sorted[R](rows: Iterable[tuple[int, R]]) -> Sequenced[R]:
    return tuple(sorted(rows, key=lambda pair: pair[0]))


def make_engine(plan: UnitPlan) -> BacktestEngine:
    """The AC-5 venue for this unit's variant, with the pinned instrument carrying the run's fees."""
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId(TRADER_ID),
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=Venue(InstrumentId.from_str(plan.instrument_id).venue.value),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(plan.starting_balance_usd, USD)],
        base_currency=USD,
        default_leverage=plan.leverage,
        fee_model=MakerTakerFeeModel(),
        # In 1.231.0 the extra latencies add to the base, and latency applies strategy to venue only.
        latency_model=LatencyModel(
            base_latency_nanos=plan.latency_ms * NS_PER_MS,
            insert_latency_nanos=0,
            update_latency_nanos=0,
            cancel_latency_nanos=0,
        ),
        book_type=BookType.L2_MBP,
        queue_position=plan.queue_position,
        liquidity_consumption=True,
        trade_execution=True,
        bar_execution=False,
        use_reduce_only=True,
    )
    engine.add_instrument(with_fees(plan.instrument_id, plan.maker_fee, plan.taker_fee))
    return engine


def run_unit(plan: UnitPlan) -> UnitResult:
    """Replay one variant over one segment and return its rows.

    Raises whatever escapes the engine or the strategy setup, and ``RecordingFailedError`` when
    the recorder could not map an event (its bundle would be incomplete); either fails the
    variant (AC-13).
    """
    started = time.monotonic()
    segment = plan.segment
    fields = {"variant": plan.variant, "segment": segment.index}
    tree = SourceTree(plan.source_root, plan.instrument_id)
    instrument_id = InstrumentId.from_str(plan.instrument_id)

    gate = PauseGate()
    gate.unpause()  # loaded, not paused: the lab never pauses
    blocks = EntryBlocks()
    rows = UnitRows()
    entry = registry_entry(plan.strategy_name)
    context = RunContext(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=plan.instrument_id,
        seed=plan.seed,
        risk=plan.risk,
        order_id_tag=f"S{segment.index}",
    )
    strategy = entry.build(entry.build_config(plan.strategy_config, context), gate, blocks, rows)
    recorder = LabRecorder(env=plan.env, run_id=UNIT_RUN_ID, instrument_id=instrument_id, rows=rows, blocks=blocks)

    def hour_done(hour: int, events: int, seconds: float) -> None:
        log.info(
            "lab_hour_replayed",
            extra={
                "fields": {
                    **fields,
                    "hour_ns": hour,
                    "events": events,
                    "wall_seconds": round(seconds, 3),
                    "rss_bytes": rss_bytes(),
                }
            },
        )

    stream = SegmentStream(tree, segment, on_hour=hour_done)
    # The run ends with the data: past it the engine would keep firing timers against a frozen
    # book. (Milestone 4 moves the end to the final synthetic batch, AC-8.)
    data_end = last_ts_init(tree.hour_file(DELTAS_TABLE, segment.hours[-1]))
    engine = make_engine(plan)
    try:
        engine.add_actor(recorder)
        engine.add_strategy(strategy)
        engine.add_data_iterator(f"segment-{segment.index}", stream.chunks())
        engine.run(start=segment.start_ns, end=data_end)
    finally:
        engine.dispose()
    if BlockReason.RECORDING_FAILED in blocks.active():
        raise RecordingFailedError(f"variant {plan.variant} segment {segment.index}: an event could not be recorded")

    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    wall = time.monotonic() - started
    log.info(
        "lab_unit_done",
        extra={"fields": {**fields, "events": stream.events, "wall_seconds": round(wall, 3), "ru_maxrss": max_rss}},
    )
    return UnitResult(
        variant=plan.variant,
        segment=segment.index,
        decisions=_sorted(rows.decisions.values()),
        risk_events=_sorted(rows.risk_events.values()),
        orders=_sorted(rows.orders.values()),
        order_events=_sorted(rows.order_events.values()),
        fills=_sorted(rows.fills.values()),
        positions=_sorted(rows.positions.values()),
        spreads=dict(rows.spreads),
        events=stream.events,
        last_ts_init=stream.last_ts_init,
        wall_seconds=wall,
        max_rss_bytes=max_rss,
    )
