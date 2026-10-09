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
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import LatencyModel, MakerTakerFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position

from lab.actors import LabRecorder, SegmentEndCloser, UnitRows
from lab.catalog import DELTAS_TABLE, SourceTree, last_ts_init
from lab.instruments import with_fees
from lab.strategies import ConfigValue, RunContext, registry_entry
from lab.stream import SegmentStream, rss_bytes
from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import FundingSample, funding_boundaries
from trading_core.lab.markouts import FillMarkouts
from trading_core.lab.segments import Segment
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import (
    Env,
    Liquidity,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    TimeInForce,
)
from trading_core.recording.rows import (
    DecisionRow,
    FillRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
    ns_to_utc,
)
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

TRADER_ID: Final = "BACKTESTER-LAB"
# Units build rows before the variant has a run id; the parent replaces this one (AC-12).
UNIT_RUN_ID: Final = UUID(int=0)
NS_PER_MS: Final = 1_000_000
CENT: Final = Decimal("0.01")


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
    # Sample the funding boundaries inside the segment (``tardis`` runs; own days carry no funding).
    funding: bool = False
    env: Env = Env.DEMO
    # The test only strategies are registered (LAB_TEST_STRATEGIES=1, AC-13).
    test_strategies: bool = False


type Sequenced[R] = tuple[tuple[int, R], ...]


@dataclass(frozen=True)
class UnitResult:
    """A unit's rows with their sequence inside the unit, the spreads at order creation, the funding
    samples (AC-9), and its cost.

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
    funding_samples: tuple[FundingSample, ...]
    # True when the last book could not close a position and the rest closed at the last mid (AC-8).
    segment_end_partial: bool
    # Each fill's markouts at 1, 5, and 30 s, by trade id within this unit (AC-10).
    markouts: tuple[FillMarkouts, ...]
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


class CloseSource(Protocol):
    """What ``settle_at_mid`` needs from the runner's closer: its close order, and a way to record a close decision."""

    @property
    def close_id(self) -> str | None:
        """The close order's client order id, once sent."""
        ...

    def record_close_decision(self, side: OrderSide, quantity: Decimal, decided_ns: int) -> UUID:
        """Record the ``segment_end`` exit decision and return its id."""
        ...


def settle_at_mid(
    rows: UnitRows, closer: CloseSource, position: Position, *, mid: Decimal, taker_fee: Decimal, at_ns: int
) -> None:
    """Close what the last book could not (AC-8): the rest of ``position`` at ``mid``, paying the taker fee.

    The rest becomes one taker fill on the runner's close order (or on a close order made here
    when the runner sent none, with its ``segment_end`` decision), and the position row is closed
    with that fill's P&L after the fee, as the engine would have booked it.
    """
    key = (position.id.value, position.ts_opened)
    found = rows.positions.get(key)
    if found is None:
        raise RecordingFailedError(f"open position {position.id} at segment end was never recorded")
    seq, row = found
    qty = position.quantity.as_decimal()
    long = row.side == PositionSide.LONG
    closing = OrderSide.SELL if long else OrderSide.BUY
    fee = (taker_fee * qty * mid).quantize(CENT, rounding=ROUND_HALF_EVEN)
    pnl = (mid - row.avg_entry_px) * qty * (1 if long else -1) - fee

    close_id = closer.close_id
    order = None if close_id is None else rows.orders.get(close_id)
    if close_id is None or order is None:
        decision = closer.record_close_decision(closing, qty, at_ns)
        close_id = f"{position.id.value}-segment-end"
        order = (
            rows.next_seq(),
            OrderRow(
                env=row.env,
                run_id=row.run_id,
                decision_id=decision,
                origin=OrderOrigin.STRATEGY,
                client_order_id=close_id,
                instrument_id=row.instrument_id,
                side=closing,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                quantity=qty,
                reduce_only=True,
                status=OrderStatus.INITIALIZED,
                last_event_ns=at_ns,
            ),
        )
    order_seq, order_row = order
    filled = order_row.filled_qty + qty
    avg_px = ((order_row.avg_px or Decimal(0)) * order_row.filled_qty + mid * qty) / filled
    rows.orders[close_id] = (
        order_seq,
        replace(
            order_row,
            quantity=max(order_row.quantity, filled),
            status=OrderStatus.FILLED,
            filled_qty=filled,
            avg_px=avg_px,
            last_event_ns=max(order_row.last_event_ns, at_ns),
        ),
    )
    trade_id = f"{close_id}-rest"
    rows.fills[trade_id] = (
        rows.next_seq(),
        FillRow(
            env=row.env,
            run_id=row.run_id,
            client_order_id=close_id,
            position_id=row.id,
            instrument_id=row.instrument_id,
            trade_id=trade_id,
            side=closing,
            quantity=qty,
            price=mid,
            fee=fee,
            fee_currency="USD",
            liquidity=Liquidity.TAKER,
            filled_ns=at_ns,
        ),
    )
    closed_before = row.peak_qty - abs(row.quantity)
    avg_exit = ((row.avg_exit_px or Decimal(0)) * closed_before + mid * qty) / row.peak_qty
    rows.positions[key] = (
        seq,
        replace(
            row,
            status=PositionStatus.CLOSED,
            quantity=Decimal(0),
            avg_exit_px=avg_exit,
            realized_pnl=row.realized_pnl + pnl,
            fees_total=row.fees_total + fee,
            closed_at=ns_to_utc(at_ns),
            last_event_ns=at_ns,
        ),
    )


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
    entry = registry_entry(plan.strategy_name, test_strategies=plan.test_strategies)
    context = RunContext(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=plan.instrument_id,
        seed=plan.seed,
        risk=plan.risk,
        order_id_tag=f"S{segment.index}",
    )
    strategy = entry.build(entry.build_config(plan.strategy_config, context), gate, blocks, rows)
    # The run ends at the final synthetic batch, just after the runner's close can reach the
    # venue (AC-8); past it the engine would keep firing timers against a frozen book.
    data_end = last_ts_init(tree.hour_file(DELTAS_TABLE, segment.hours[-1]))
    final_ts = data_end + plan.latency_ms * NS_PER_MS + NS_PER_MS
    closer = SegmentEndCloser(
        strategy=strategy,
        gate=gate,
        rows=rows,
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=instrument_id,
        close_at_ns=data_end,
    )
    recorder = LabRecorder(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=instrument_id,
        rows=rows,
        blocks=blocks,
        funding_boundaries=funding_boundaries(segment.start_ns, data_end) if plan.funding else (),
    )

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

    stream = SegmentStream(tree, segment, on_hour=hour_done, final_ts_init=final_ts)
    engine = make_engine(plan)
    try:
        engine.add_actor(recorder)
        engine.add_strategy(strategy)
        engine.add_actor(closer)
        engine.add_data_iterator(f"segment-{segment.index}", stream.chunks())
        engine.run(start=segment.start_ns, end=final_ts)
        if BlockReason.RECORDING_FAILED in blocks.active():
            raise RecordingFailedError(
                f"variant {plan.variant} segment {segment.index}: an event could not be recorded"
            )
        markouts = recorder.markouts(data_end)
        rest = engine.cache.positions_open(instrument_id=instrument_id)
        partial = bool(rest)
        for position in rest:
            mid = stream.last_mid()
            if mid is None:
                raise RecordingFailedError(f"segment {segment.index}: a position is open but the last book is empty")
            settle_at_mid(rows, closer, position, mid=mid, taker_fee=plan.taker_fee, at_ns=final_ts)
    finally:
        engine.dispose()
    if partial:
        log.warning("lab_segment_end_partial", extra={"fields": {**fields, "positions": len(rest)}})

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
        funding_samples=tuple(rows.funding_samples),
        segment_end_partial=partial,
        markouts=tuple(markouts),
        events=stream.events,
        last_ts_init=stream.last_ts_init,
        wall_seconds=wall,
        max_rss_bytes=max_rss,
    )
