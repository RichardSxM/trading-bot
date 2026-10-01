"""Subscribes to the market data the streaming writer records."""

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId


class RecordingConfig(ActorConfig, frozen=True):
    instrument_ids: list[str]


class RecordingActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    def __init__(self, config: RecordingConfig) -> None:
        super().__init__(config)
        self._instrument_ids = [InstrumentId.from_str(i) for i in config.instrument_ids]

    def on_start(self) -> None:
        # The kernel's streaming writer receives everything on the bus; these
        # subscriptions make the data flow. Hourly Parquet conversion is feature 8.
        for instrument_id in self._instrument_ids:
            self.subscribe_order_book_deltas(instrument_id, BookType.L2_MBP)
            self.subscribe_trade_ticks(instrument_id)
