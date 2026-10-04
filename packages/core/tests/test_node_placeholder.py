"""covers: spec 0006 AC-3 (the timed entry and its warmup), AC-4 (sizing), AC-5 (a refusal is recorded),
AC-6 (the stop rests once the entry fills), AC-8 (the time exit), AC-9 (a stop fill ends the round trip).

Runs ``PlaceholderTimed`` in a BacktestEngine on synthetic quotes for a PF_XBTUSD shaped instrument
(USD settled linear perpetual, tick 1, size step 0.0001). Needs the ``node`` extra.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity

from trading_core.node.placeholder import Phase, PlaceholderConfig, PlaceholderTimed
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import (
    Env,
    Intent,
    OrderEventKind,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskEventKind,
    RiskVerdict,
)
from trading_core.recording.rows import (
    AccountSnapshotRow,
    DecisionRow,
    FillRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    PositionSnapshotRow,
    RiskEventRow,
)
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks

START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
INSTRUMENT = CryptoPerpetual(
    instrument_id=INSTRUMENT_ID,
    raw_symbol=Symbol("PF_XBTUSD"),
    base_currency=BTC,
    quote_currency=USD,
    settlement_currency=USD,
    is_inverse=False,
    price_precision=0,
    size_precision=4,
    price_increment=Price.from_str("1"),
    size_increment=Quantity.from_str("0.0001"),
    margin_init=Decimal("0.02"),
    margin_maint=Decimal("0.01"),
    maker_fee=Decimal("0.0002"),
    taker_fee=Decimal("0.0005"),
    ts_event=0,
    ts_init=0,
)


def _ns(at):
    return int(at.timestamp() * 1_000_000_000)


def _quotes(prices, every_s=10):
    """One quote every ``every_s`` seconds from START; ``prices(t)`` gives the mid at t minutes."""
    ticks, at = [], START
    for step in range(int(prices.minutes * 60 / every_s)):
        mid = prices(step * every_s / 60)
        if mid is None:  # no quote this step
            at += timedelta(seconds=every_s)
            continue
        ticks.append(
            QuoteTick(
                instrument_id=INSTRUMENT_ID,
                bid_price=Price(mid - 1, 0),
                ask_price=Price(mid + 1, 0),
                bid_size=Quantity.from_str("5.0000"),
                ask_size=Quantity.from_str("5.0000"),
                ts_event=_ns(at),
                ts_init=_ns(at),
            )
        )
        at += timedelta(seconds=every_s)
    return ticks


class Prices:
    def __init__(self, minutes, mid):
        self.minutes = minutes
        self.mid = mid

    def __call__(self, t):
        return self.mid(t)


def _run(prices, start_offset_min=0, **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(1000, USD)],
        base_currency=USD,
    )
    engine.add_instrument(INSTRUMENT)
    ticks = _quotes(prices)
    engine.add_data(ticks[int(start_offset_min * 6) :])
    run_id = uuid.uuid7()
    values = {
        "env": "demo",
        "run_id": str(run_id),
        "instrument_id": str(INSTRUMENT_ID),
        "interval_min": 15,
        "hold_min": 10,
        "lookback_min": 5,
        "stop_pct": Decimal("0.3"),
        "notional_usd": Decimal(100),
        "max_order_notional_usd": Decimal(250),
        "max_leverage": Decimal(2),
        "stale_quote_s": Decimal(10),
        "stop_accept_timeout_s": Decimal(5),
    } | overrides
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    engine.add_actor(
        RecordingActor(env=Env.DEMO, run_id=run_id, instrument_id=INSTRUMENT_ID, record=records.append, blocks=blocks)
    )
    config = PlaceholderConfig(**values)  # type: ignore[arg-type]  # each value has its own type
    strategy = PlaceholderTimed(config, PauseGate(), blocks, records.append)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows):
    return [r for r in rows if isinstance(r, DecisionRow)]


def _risk_events(rows):
    return [r for r in rows if isinstance(r, RiskEventRow)]


def test_a_rising_market_buys_at_the_boundary_rests_a_stop_and_closes_on_time():
    engine, strategy, rows = _run(Prices(29, lambda t: 85000 + int(t * 10)))

    enter, exit_ = _decisions(rows)
    assert (enter.intent, enter.side, enter.reason, enter.risk_verdict) == (
        Intent.ENTER,
        OrderSide.BUY,
        "timed_entry",
        RiskVerdict.APPROVED,
    )
    # 2 s after 12:15, from bars closed by then.
    assert enter.decided_ns == _ns(START + timedelta(minutes=15, seconds=2))
    assert enter.inputs["quantity"] == "0.0011"  # 100 USD at about 85150, floored to 0.0001
    assert (exit_.intent, exit_.side, exit_.reason) == (Intent.EXIT, OrderSide.SELL, "time")
    assert _risk_events(rows) == []

    orders = engine.cache.orders()
    assert [o.order_type for o in orders] == [OrderType.MARKET, OrderType.STOP_MARKET, OrderType.MARKET]
    entry, stop, exit_order = orders
    assert stop.is_reduce_only
    assert exit_order.is_reduce_only
    assert stop.trigger_price.as_decimal() < entry.avg_px  # below a long
    assert stop.is_closed  # cancelled once flat
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_stop_fill_ends_the_round_trip_without_a_time_exit():
    # Rising into the 12:15 entry, then a 1 % drop at 12:18.
    engine, strategy, rows = _run(Prices(29, lambda t: 85000 + int(t * 10) if t < 18 else 84000))

    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER]
    stop = next(o for o in engine.cache.orders() if o.order_type == OrderType.STOP_MARKET)
    assert stop.is_closed
    assert stop.filled_qty == stop.quantity
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_too_few_bars_at_the_boundary_skips_without_a_decision():
    # Quotes from 12:12 only: 3 bars at 12:15, fewer than lookback + 1.
    engine, strategy, rows = _run(Prices(20, lambda t: 85000), start_offset_min=12)

    assert _decisions(rows) == []
    assert engine.cache.orders() == []
    assert strategy.phase == Phase.FLAT


def test_a_refused_entry_records_the_decision_and_a_limit_hit_and_places_nothing():
    engine, _, rows = _run(Prices(20, lambda t: 85000 - int(t * 10)), notional_usd=Decimal(300))

    (decision,) = _decisions(rows)
    assert (decision.side, decision.risk_verdict, decision.risk_reason) == (
        OrderSide.SELL,
        RiskVerdict.REJECTED,
        "notional_cap",
    )
    (event,) = _risk_events(rows)
    assert (event.kind, event.event_key) == (RiskEventKind.LIMIT_HIT, f"limit_hit:{decision.id}")
    assert engine.cache.orders() == []


def _of(rows, cls):
    return [r for r in rows if isinstance(r, cls)]


def test_the_recording_actor_queues_every_order_fill_and_position_row_in_order(caplog):
    with caplog.at_level(logging.INFO, logger="trading_core.node.recording"):
        engine, _, rows = _run(Prices(29, lambda t: 85000 + int(t * 10)))
    enter, exit_ = _decisions(rows)

    # Orders: the latest row per order carries its decision, and the stop names its parent.
    latest = {r.client_order_id: r for r in _of(rows, OrderRow)}
    entry, stop, exit_order = (latest[o.client_order_id.value] for o in engine.cache.orders())
    assert (entry.decision_id, stop.decision_id, exit_order.decision_id) == (enter.id, enter.id, exit_.id)
    assert stop.parent_client_order_id == entry.client_order_id
    assert (entry.status, stop.status, exit_order.status) == (
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.FILLED,
    )
    assert entry.avg_px is not None
    assert entry.trigger_price is None
    assert stop.trigger_price is not None

    # Every order event once, the fills among them.
    kinds = [r.kind for r in _of(rows, OrderEventRow)]
    assert kinds.count(OrderEventKind.ORDER_FILLED) == 2
    assert len({r.event_id for r in _of(rows, OrderEventRow)}) == len(kinds)

    # Each fill first without its position, then once with it, after the position row.
    fills = _of(rows, FillRow)
    trades = {f.trade_id for f in fills}
    assert len(trades) == 2
    (position,) = {r.id for r in _of(rows, PositionRow)}
    for trade in trades:
        assert [f.position_id for f in fills if f.trade_id == trade] == [None, position]
    first_position_row = next(i for i, r in enumerate(rows) if isinstance(r, PositionRow))
    first_linked = next(i for i, r in enumerate(rows) if isinstance(r, FillRow) and r.position_id is not None)
    assert first_position_row < first_linked

    final = _of(rows, PositionRow)[-1]
    assert (final.status, final.quantity) == (PositionStatus.CLOSED, 0)
    assert final.avg_exit_px is not None
    assert final.fees_total > 0

    # Snapshots on fills and on the 60 s timer, the position ones only while open.
    assert len(_of(rows, AccountSnapshotRow)) >= 29
    assert {s.position_id for s in _of(rows, PositionSnapshotRow)} == {position}

    seen = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "fill_seen"]
    assert sorted(r.fields["trade_id"] for r in seen) == sorted(trades)


def test_steady_quotes_never_raise_stale_feed():
    _, _, rows = _run(Prices(20, lambda t: 85000))

    assert [e for e in _risk_events(rows) if e.kind == RiskEventKind.STALE_FEED] == []


def test_a_quote_gap_raises_one_stale_feed_event_and_clears():
    # No quotes from minute 5 to minute 7, then quotes again.
    _, strategy, rows = _run(Prices(10, lambda t: None if 5 <= t < 7 else 85000))

    (event,) = [e for e in _risk_events(rows) if e.kind == RiskEventKind.STALE_FEED]
    assert event.event_key.startswith("stale_feed:")
    assert strategy._blocks.active() == ()  # cleared once quotes came back


class _EmptyCache:
    def order(self, client_order_id):
        return None


def test_an_init_event_before_the_order_is_cached_still_records_the_order():
    """On a live node OrderInitialized arrives before the order is in the cache (seen 2026-10-04)."""
    from nautilus_trader.common.component import TestClock
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide as NautilusSide
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

    from trading_core.node.recording import order_facts, order_for

    factory = OrderFactory(TestIdStubs.trader_id(), TestIdStubs.strategy_id(), TestClock())
    order = factory.market(
        INSTRUMENT_ID, NautilusSide.SELL, Quantity.from_str("0.0011"), tags=[f"decision:{uuid.uuid7()}"]
    )

    facts = order_facts(order_for(_EmptyCache(), order.init_event))

    assert (facts.client_order_id, facts.status, facts.side) == (order.client_order_id.value, "INITIALIZED", "SELL")
    assert list(facts.tags) == order.tags
    with pytest.raises(LookupError):  # any later event needs the cached order
        order_for(_EmptyCache(), TestEventStubs.order_submitted(order))
