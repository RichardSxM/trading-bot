"""The two custom data types of the raw context feed (spec 0009 AC-3): ``OpenInterestUpdate`` and
``LiquidationTick``.

Both are registered with Nautilus's Arrow serializer when this module is imported, which must be
before a node's streaming writer is built (it reads the schema list once). Prices and sizes are
stored as text, never floats; the catalog tables are ``custom_open_interest_update`` and
``custom_liquidation_tick`` (the Milestone 0 gate).
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from trading_core.catalog.context import register_context_arrow
from trading_core.marketdata.context import AggressorSide, LiquidationKind


class OpenInterestUpdate(Data):  # type: ignore[misc]  # Data is Cython with no stubs
    """How many contracts are open on an instrument, from Kraken's ``ticker`` feed."""

    def __init__(self, instrument_id: InstrumentId, value: Quantity, ts_event: int, ts_init: int) -> None:
        self.instrument_id = instrument_id
        self.value = value
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        """Kraken's time of the update, in nanoseconds."""
        return self._ts_event

    @property
    def ts_init(self) -> int:
        """When this node received it: the time it became known."""
        return self._ts_init

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OpenInterestUpdate):
            return NotImplemented
        return (self.instrument_id, self.value, self._ts_event, self._ts_init) == (
            other.instrument_id,
            other.value,
            other._ts_event,
            other._ts_init,
        )

    def __hash__(self) -> int:
        return hash((self.instrument_id, str(self.value), self._ts_event, self._ts_init))

    def __repr__(self) -> str:
        return (
            f"OpenInterestUpdate({self.instrument_id}, {self.value}, "
            f"ts_event={self._ts_event}, ts_init={self._ts_init})"
        )


class LiquidationTick(Data):  # type: ignore[misc]  # Data is Cython with no stubs
    """One forced or block trade from Kraken's ``trade`` feed: ``kind`` says which (``LiquidationKind``)."""

    def __init__(
        self,
        instrument_id: InstrumentId,
        trade_id: str,
        kind: LiquidationKind,
        aggressor_side: AggressorSide,
        price: Price,
        size: Quantity,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self.instrument_id = instrument_id
        self.trade_id = trade_id
        self.kind = kind
        self.aggressor_side = aggressor_side
        self.price = price
        self.size = size
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        """Kraken's trade time, in nanoseconds."""
        return self._ts_event

    @property
    def ts_init(self) -> int:
        """When this node received it: the time it became known."""
        return self._ts_init

    def _key(self) -> tuple[object, ...]:
        return (
            self.instrument_id,
            self.trade_id,
            self.kind,
            self.aggressor_side,
            str(self.price),
            str(self.size),
            self._ts_event,
            self._ts_init,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LiquidationTick):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return (
            f"LiquidationTick({self.instrument_id}, {self.trade_id}, {self.kind}, {self.aggressor_side}, "
            f"{self.price}, {self.size}, ts_event={self._ts_event}, ts_init={self._ts_init})"
        )


def context_data_type(cls: type[Data], instrument_id: InstrumentId) -> DataType:
    """The ``DataType`` every publisher, subscriber, and the lab use for a custom context type (AC-3)."""
    return DataType(cls, metadata={"instrument_id": instrument_id.value})


# Registered on import, before any node's streaming writer is built (the Arrow side lives in catalog/).
register_context_arrow(OpenInterestUpdate, LiquidationTick)
