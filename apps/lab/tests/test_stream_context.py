"""covers: spec 0009 AC-13 (the hour stream merges the context tables in a fixed tie order, each
segment's warmup, and no look ahead: a replayed strategy's context equals a book fed only the rows
known so far)."""

from decimal import Decimal
from typing import Any

import pyarrow.parquet as pq
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core import nautilus_pyo3 as p3
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import (
    FundingRateUpdate,
    IndexPriceUpdate,
    MarkPriceUpdate,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from lab.catalog import FUNDING_TABLE, INDEX_TABLE, MARK_TABLE
from lab.stream import SegmentStream
from trading_core.lab.segments import Segment
from trading_core.marketdata.context import ContextBook, MaxAges
from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate

from .conftest import H17, HOUR, INSTRUMENT, Row, snapshot, write_hour

S = 1_000_000_000
H18 = H17 + HOUR
IID = InstrumentId.from_str(INSTRUMENT)
BID, ASK = p3.OrderSide.BUY, p3.OrderSide.SELL
UPDATE = p3.BookAction.UPDATE
BOOK = [(BID, "100", "1"), (ASK, "101", "1")]
TABLES = {MarkPriceUpdate: MARK_TABLE, IndexPriceUpdate: INDEX_TABLE, FundingRateUpdate: FUNDING_TABLE}


def write_context(tree, rows):
    """Write context rows of one class into their hour files, as the compactor would."""
    cls = type(rows[0])
    by_hour: dict[int, list[Any]] = {}
    for row in rows:
        by_hour.setdefault(row.ts_init - row.ts_init % HOUR, []).append(row)
    for hour, hour_rows in by_hour.items():
        path = tree.hour_file(TABLES[cls], hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(ArrowSerializer.serialize_batch(hour_rows, data_cls=cls), path)


def mark(ts, value=83_597):
    return MarkPriceUpdate(IID, Price(value, 0), ts, ts)


def index(ts, value=83_587):
    return IndexPriceUpdate(IID, Price(value, 0), ts, ts)


def funding(ts, rate="0.0001"):
    return FundingRateUpdate(IID, Decimal(rate), ts, ts, next_funding_ns=ts - ts % HOUR + HOUR)


def _events(stream):
    return [event for chunk in stream.chunks() for event in chunk]


def test_at_one_nanosecond_book_trades_then_mark_index_funding(tmp_path):
    ts = H17 + 20 * S
    tree = write_hour(tmp_path, H17, [*snapshot(H17, BOOK), Row(UPDATE, BID, "100", "4", ts)], [("101", "1", ts)])
    write_context(tree, [funding(ts)])
    write_context(tree, [index(ts)])
    write_context(tree, [mark(ts), mark(ts, 83_598)])

    events = [e for e in _events(SegmentStream(tree, Segment(1, (H17,)))) if e.ts_init == ts]

    names = {OrderBookDeltas: "book", QuoteTick: "quote", TradeTick: "trade"}
    names |= {MarkPriceUpdate: "mark", IndexPriceUpdate: "index", FundingRateUpdate: "funding"}
    assert [names[type(e)] for e in events] == ["book", "quote", "trade", "mark", "mark", "index", "funding"]
    assert [str(e.value) for e in events if isinstance(e, MarkPriceUpdate)] == ["83597", "83598"]  # file order
    (rate,) = [e.rate for e in events if isinstance(e, FundingRateUpdate)]
    assert rate == Decimal("0.0001")  # a Decimal, as live


def test_the_warmup_is_the_context_rows_known_before_the_segment(tmp_path):
    tree = write_hour(tmp_path, H18, [*snapshot(H18, BOOK)], [("101", "1", H18 + S)])
    write_context(tree, [funding(H17 - 1), funding(H17 + 30 * 60 * S), funding(H18 - 1), funding(H18)])
    write_context(tree, [mark(H17 + 10 * S)])
    stream = SegmentStream(tree, Segment(1, (H18,)))

    warm = stream.warmup(HOUR)

    assert [(type(e).__name__, e.ts_init) for e in warm] == [
        ("MarkPriceUpdate", H17 + 10 * S),
        ("FundingRateUpdate", H17 + 30 * 60 * S),
        ("FundingRateUpdate", H18 - 1),
    ]
    assert stream.warmup(0) == []
    # The segment's own hour still holds its rows: the one at the segment start is not warmup.
    assert [e.ts_init for e in _events(stream) if isinstance(e, FundingRateUpdate)] == [H18]


class _Probe(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Every 5 s (1 ns past the second, so no row shares its time), compares the strategy's context
    with a book fed only the rows known by then. At one nanosecond the book comes before the context
    (the tie order), so a check at a row's own time would read it mid nanosecond."""

    def __init__(self, strategy, rows, window_start):
        super().__init__(ActorConfig(component_id="Probe"))
        self._strategy = strategy
        self._rows = rows
        self._window_start = window_start
        self.checked = 0

    def on_start(self):
        self.clock.set_timer_ns("probe", 5 * S, H18 + 1, 0, self._check)

    def _check(self, _event):
        now = self.clock.timestamp_ns()
        book = ContextBook(AGES, 60 * S, liquidations_available=False)
        for row in self._rows:
            if self._window_start <= row.ts_init <= now:
                if isinstance(row, MarkPriceUpdate):
                    book.apply_mark(row.value.as_decimal(), row.ts_init)
                elif isinstance(row, IndexPriceUpdate):
                    book.apply_index(row.value.as_decimal(), row.ts_init)
                else:
                    book.apply_funding(row.rate, row.next_funding_ns, row.ts_init)
        assert self._strategy.market_context() == book.snapshot(now)
        self.checked += 1


AGES = MaxAges.from_seconds(mark=30, index=30, open_interest=30, funding=7_200)
INSTRUMENT_DEF = CryptoPerpetual(
    instrument_id=IID,
    raw_symbol=Symbol("PF_XBTUSD"),
    base_currency=BTC,
    quote_currency=USD,
    settlement_currency=USD,
    is_inverse=False,
    price_precision=0,
    size_precision=4,
    price_increment=Price.from_str("1"),
    size_increment=Quantity.from_str("0.0001"),
    ts_event=0,
    ts_init=0,
)


def test_a_replayed_strategy_never_sees_a_row_before_its_ts_init(tmp_path):
    warmup = HOUR
    book_rows = [*snapshot(H18, BOOK)]
    for i in range(1, 120):
        book_rows.append(Row(UPDATE, BID, "100", str(1 + i % 5), H18 + i * 15 * S))
    tree = write_hour(tmp_path, H18, book_rows, [("101", "1", H18 + 5 * S)])
    marks = [mark(H18 - 40 * S)] + [mark(H18 + i * 7 * S + 3, 83_590 + i % 9) for i in range(250)]
    indexes = [index(H18 + i * 11 * S, 83_580 + i % 4) for i in range(160)]
    rates = [funding(H17 - 1), funding(H17 + 50 * 60 * S, "0.0002"), funding(H18 + 600 * S + 7, "0.0003")]
    write_context(tree, marks)
    write_context(tree, indexes)
    write_context(tree, rates)
    rows = sorted([*marks, *indexes, *rates], key=lambda r: r.ts_init)

    strategy = KfsStrategy(KfsStrategyConfig(instrument_id=INSTRUMENT, use_market_context=True), PauseGate())
    stream = SegmentStream(tree, Segment(1, (H18,)))
    strategy.warm_market_context(stream.warmup(warmup))
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(1000, USD)])
    engine.add_instrument(INSTRUMENT_DEF)
    engine.add_strategy(strategy)
    probe = _Probe(strategy, rows, H18 - warmup)
    engine.add_actor(probe)
    engine.add_data_iterator("segment-1", stream.chunks())
    engine.run(start=H18, end=H18 + HOUR - 1)

    assert probe.checked > 100
    # The 23:00 rate before the warmup window is never known; the warmup's 17:50 one is from the start.
    ctx = strategy.market_context()
    assert ctx.funding_rate is not None
    assert ctx.funding_rate.value == Decimal("0.0003")


def test_open_interest_and_liquidations_follow_funding_as_custom_data(tmp_path):
    from nautilus_trader.model.data import CustomData
    from nautilus_trader.model.objects import Quantity

    from lab.catalog import LIQUIDATION_TABLE, OPEN_INTEREST_TABLE
    from trading_core.marketdata.context import AggressorSide, LiquidationKind
    from trading_core.node.context_types import LiquidationTick, OpenInterestUpdate

    ts = H17 + 20 * S
    tree = write_hour(tmp_path, H17, [*snapshot(H17, BOOK)], [("101", "1", ts)])
    write_context(tree, [funding(ts)])
    for table, row in (
        (OPEN_INTEREST_TABLE, OpenInterestUpdate(IID, Quantity.from_str("2423.1378"), ts, ts)),
        (
            LIQUIDATION_TABLE,
            LiquidationTick(
                IID,
                "u1",
                LiquidationKind.LIQUIDATION,
                AggressorSide.SELL,
                Price(83_500, 0),
                Quantity.from_str("0.5000"),
                ts,
                ts,
            ),
        ),
    ):
        path = tree.hour_file(table, H17)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(ArrowSerializer.serialize_batch([row], data_cls=type(row)), path)

    events = [e for e in _events(SegmentStream(tree, Segment(1, (H17,)))) if e.ts_init == ts]

    kinds = [type(e.data).__name__ if isinstance(e, CustomData) else type(e).__name__ for e in events]
    assert kinds == ["TradeTick", "FundingRateUpdate", "OpenInterestUpdate", "LiquidationTick"]
    custom = [e for e in events if isinstance(e, CustomData)]
    assert all(e.data_type.metadata == {"instrument_id": INSTRUMENT} for e in custom)
    assert str(custom[0].data.value) == "2423.1378"
