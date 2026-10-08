"""``LabRecorder`` and ``UnitRows``: a unit's rows, built exactly as the paper loop builds them (spec 0008 AC-7).

``LabRecorder`` is the trader's ``RecordingActor`` with only the order and position
subscriptions (no account snapshots, no timer, no instrument row: the replay writes that once,
AC-11). It goes through the same fact builders and pure mappers, and hands every row to
``UnitRows`` instead of a writer thread. It also notes the quoted spread when each order is
created, for the loser check (AC-14).

``UnitRows`` collapses what the writer would upsert: the newest row per order and per position,
one fill per trade (the one linked to its position, when it was), one event per event id. Every
row keeps the order it was first seen in, the "sequence within the unit" of AC-12.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from nautilus_trader.model.events import OrderInitialized
from nautilus_trader.model.identifiers import InstrumentId

from trading_core.node.recording import RecordingActor
from trading_core.recording.enums import Env
from trading_core.recording.rows import DecisionRow, FillRow, OrderEventRow, OrderRow, PositionRow, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.strategy.blocks import EntryBlocks

# Rows a replay never writes: the instrument goes in once before the bundle, snapshots are paper only.
_IGNORED = frozenset(
    {WriteCall.UPSERT_INSTRUMENT, WriteCall.RECORD_POSITION_SNAPSHOT, WriteCall.RECORD_ACCOUNT_SNAPSHOT}
)


@dataclass
class UnitRows:
    """Collects one unit's rows from the strategy and the recorder; called like the writer's ``put``."""

    decisions: dict[object, tuple[int, DecisionRow]] = field(default_factory=dict)
    risk_events: dict[str, tuple[int, RiskEventRow]] = field(default_factory=dict)
    orders: dict[str, tuple[int, OrderRow]] = field(default_factory=dict)
    order_events: dict[tuple[str, object], tuple[int, OrderEventRow]] = field(default_factory=dict)
    fills: dict[str, tuple[int, FillRow]] = field(default_factory=dict)
    positions: dict[tuple[str, int], tuple[int, PositionRow]] = field(default_factory=dict)
    # The quoted spread (best ask - best bid, USD) when each order was created, by client order id.
    spreads: dict[str, Decimal] = field(default_factory=dict)
    _seq: int = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def __call__(self, item: WriteItem) -> None:
        """Take one queued write, as ``RecordingWriter.put`` would."""
        row = item.row
        match item.call:
            case WriteCall.RECORD_DECISION if isinstance(row, DecisionRow):
                self.decisions.setdefault(row.id, (self._next(), row))
            case WriteCall.RECORD_RISK_EVENT if isinstance(row, RiskEventRow):
                self.risk_events.setdefault(row.event_key, (self._next(), row))
            case WriteCall.UPSERT_ORDER if isinstance(row, OrderRow):
                seen = self.orders.get(row.client_order_id)
                if seen is None:
                    self.orders[row.client_order_id] = (self._next(), row)
                elif row.last_event_ns >= seen[1].last_event_ns:
                    self.orders[row.client_order_id] = (seen[0], row)
            case WriteCall.RECORD_ORDER_EVENT if isinstance(row, OrderEventRow):
                self.order_events.setdefault((row.client_order_id, row.event_id), (self._next(), row))
            case WriteCall.RECORD_FILL if isinstance(row, FillRow):
                seen_fill = self.fills.get(row.trade_id)
                if seen_fill is None:
                    self.fills[row.trade_id] = (self._next(), row)
                elif seen_fill[1].position_id is None and row.position_id is not None:
                    self.fills[row.trade_id] = (seen_fill[0], row)
            case WriteCall.UPSERT_POSITION if isinstance(row, PositionRow):
                key = (row.position_ref, row.opened_ns)
                seen_position = self.positions.get(key)
                if seen_position is None:
                    self.positions[key] = (self._next(), row)
                elif row.last_event_ns >= seen_position[1].last_event_ns:
                    self.positions[key] = (seen_position[0], row)
            case call if call in _IGNORED:
                pass
            case call:
                raise TypeError(f"unexpected write {call} with {type(row).__name__}")


class LabRecorder(RecordingActor):
    """The paper loop's recording actor, minus snapshots, plus the spread at order creation."""

    def __init__(
        self, *, env: Env, run_id: UUID, instrument_id: InstrumentId, rows: UnitRows, blocks: EntryBlocks
    ) -> None:
        super().__init__(env=env, run_id=run_id, instrument_id=instrument_id, record=rows, blocks=blocks)
        self._rows = rows
        self._lab_instrument_id = instrument_id

    def on_start(self) -> None:
        """Subscribe to every order and position event (the instrument and snapshots are not replay rows)."""
        self.msgbus.subscribe(topic="events.order.*", handler=self._guarded(self._on_lab_order_event))
        self.msgbus.subscribe(topic="events.position.*", handler=self._guarded(self._on_position_event))

    def on_stop(self) -> None:
        """Nothing to stop: the lab starts no timer."""

    def _on_lab_order_event(self, event: object) -> None:
        if isinstance(event, OrderInitialized):
            quote = self.cache.quote_tick(self._lab_instrument_id)
            if quote is not None:
                spread = quote.ask_price.as_decimal() - quote.bid_price.as_decimal()
                self._rows.spreads.setdefault(event.client_order_id.value, spread)
        self._on_order_event(event)
