"""The market context tables in Arrow (spec 0009 AC-1, AC-13): reading the native ones back, and the
two custom types' schemas, registered by ``trading_core.node.context_types`` on import.

NautilusTrader 1.231.0 writes ``MarkPriceUpdate`` and ``IndexPriceUpdate`` to Arrow but cannot read
them back (``ArrowSerializer.deserialize`` raises ``NotImplementedError``). Their ``value`` column is
the Rust ``Price``: a 16 byte little endian fixed point integer, at the precision in the file's
``price_precision`` metadata.
"""

from decimal import Decimal
from functools import partial
from typing import Any, Final

import pyarrow as pa
from nautilus_trader.model.data import FundingRateUpdate, IndexPriceUpdate, MarkPriceUpdate
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer, register_arrow

from trading_core.marketdata.context import AggressorSide, LiquidationKind


def read_price_updates[T: (MarkPriceUpdate, IndexPriceUpdate)](cls: type[T], table: Any) -> list[T]:
    """The rows of a ``mark_price_update`` or ``index_price_update`` table, in file order."""
    metadata = table.schema.metadata or {}
    instrument_id = InstrumentId.from_str(metadata[b"instrument_id"].decode())
    precision = int(metadata[b"price_precision"])
    values = table.column("value").to_pylist()
    ts_event = table.column("ts_event").to_pylist()
    ts_init = table.column("ts_init").to_pylist()
    return [
        cls(instrument_id, Price.from_raw(int.from_bytes(raw, "little", signed=True), precision), event, init)
        for raw, event, init in zip(values, ts_event, ts_init, strict=True)
    ]


def read_funding_updates(table: Any) -> list[FundingRateUpdate]:
    """The rows of a ``funding_rate_update`` table, in file order, with the rate as a ``Decimal`` as live.

    ``ArrowSerializer`` gives the rate back as text.
    """
    return [
        FundingRateUpdate(
            u.instrument_id, Decimal(str(u.rate)), u.ts_event, u.ts_init, next_funding_ns=u.next_funding_ns
        )
        for u in ArrowSerializer.deserialize(FundingRateUpdate, table)
    ]


OPEN_INTEREST_SCHEMA: Final = pa.schema(
    [
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("value", pa.string(), nullable=False),
        pa.field("ts_event", pa.uint64(), nullable=False),
        pa.field("ts_init", pa.uint64(), nullable=False),
    ]
)
LIQUIDATION_SCHEMA: Final = pa.schema(
    [
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("trade_id", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("aggressor_side", pa.string(), nullable=False),
        pa.field("price", pa.string(), nullable=False),
        pa.field("size", pa.string(), nullable=False),
        pa.field("ts_event", pa.uint64(), nullable=False),
        pa.field("ts_init", pa.uint64(), nullable=False),
    ]
)


def _open_interest_batch(items: list[Any]) -> Any:
    rows = [
        {"instrument_id": i.instrument_id.value, "value": str(i.value), "ts_event": i.ts_event, "ts_init": i.ts_init}
        for i in items
    ]
    return pa.RecordBatch.from_pylist(rows, schema=OPEN_INTEREST_SCHEMA)


def _open_interest_rows(cls: type[Any], table: Any) -> list[Any]:
    return [
        cls(InstrumentId.from_str(r["instrument_id"]), Quantity.from_str(r["value"]), r["ts_event"], r["ts_init"])
        for r in table.to_pylist()
    ]


def _liquidation_batch(items: list[Any]) -> Any:
    rows = [
        {
            "instrument_id": i.instrument_id.value,
            "trade_id": i.trade_id,
            "kind": i.kind.value,
            "aggressor_side": i.aggressor_side.value,
            "price": str(i.price),
            "size": str(i.size),
            "ts_event": i.ts_event,
            "ts_init": i.ts_init,
        }
        for i in items
    ]
    return pa.RecordBatch.from_pylist(rows, schema=LIQUIDATION_SCHEMA)


def _liquidation_rows(cls: type[Any], table: Any) -> list[Any]:
    return [
        cls(
            InstrumentId.from_str(r["instrument_id"]),
            r["trade_id"],
            LiquidationKind(r["kind"]),
            AggressorSide(r["aggressor_side"]),
            Price.from_str(r["price"]),
            Quantity.from_str(r["size"]),
            r["ts_event"],
            r["ts_init"],
        )
        for r in table.to_pylist()
    ]


def register_context_arrow(open_interest_cls: type[Any], liquidation_cls: type[Any]) -> None:
    """Register ``OpenInterestUpdate`` and ``LiquidationTick`` with Nautilus's Arrow serializer: prices and
    sizes as text, never floats."""
    register_arrow(
        open_interest_cls,
        OPEN_INTEREST_SCHEMA,
        encoder=lambda item: _open_interest_batch([item]),
        decoder=partial(_open_interest_rows, open_interest_cls),
        batch_encoder=_open_interest_batch,
    )
    register_arrow(
        liquidation_cls,
        LIQUIDATION_SCHEMA,
        encoder=lambda item: _liquidation_batch([item]),
        decoder=partial(_liquidation_rows, liquidation_cls),
        batch_encoder=_liquidation_batch,
    )
