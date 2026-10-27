"""covers: spec 0017 AC-22 (``BookImbalance`` on the node: the 15 second timer aligned to the epoch, depth
read from the cache's L2 book, trade flow from trade ticks, no bars subscribed, and no signal without a book).

Runs ``BookImbalance`` in a BacktestEngine on an L2_MBP venue with a synthetic book snapshot, quotes, and
trades. Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, BookAction, BookType, OmsType, RecordFlag
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.identifiers import TradeId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_placeholder import INSTRUMENT, INSTRUMENT_ID, START, _ns
from test_node_scalp_base import _decisions, _quotes

from trading_core.node.book_imbalance import BookImbalance, BookImbalanceConfig
from trading_core.node.funding import FundingLedger
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env, Intent, OrderSide
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.book_imbalance import BUCKET_NS

D = Decimal
MID = 85_000


def _snapshot(bid_size, ask_size):
    """Five levels a side around 84,999 / 85,001, the sizes given (None: no levels on that side), at START."""
    at = _ns(START)
    deltas = [OrderBookDelta.clear(INSTRUMENT_ID, 0, at, at)]
    for i in range(5):
        for side, price, size in (
            (NautilusOrderSide.BUY, MID - 1 - i, bid_size),
            (NautilusOrderSide.SELL, MID + 1 + i, ask_size),
        ):
            if size is None:
                continue
            order = BookOrder(side, Price(price, 0), Quantity.from_str(size), price)
            deltas.append(OrderBookDelta(INSTRUMENT_ID, BookAction.ADD, order, RecordFlag.F_SNAPSHOT, 0, at, at))
    last = deltas[-1]
    deltas[-1] = OrderBookDelta(
        INSTRUMENT_ID, last.action, last.order, RecordFlag.F_SNAPSHOT | RecordFlag.F_LAST, 0, at, at
    )
    return OrderBookDeltas(INSTRUMENT_ID, deltas)


def _buys(seconds, every_s=5):
    """A small buyer aggressor trade every ``every_s`` seconds from START + 1 s."""
    ticks = []
    for n, s in enumerate(range(1, seconds, every_s)):
        at = _ns(START + timedelta(seconds=s))
        ticks.append(
            TradeTick(
                INSTRUMENT_ID,
                Price(MID + 1, 0),
                Quantity.from_str("0.0010"),
                AggressorSide.BUYER,
                TradeId(f"t{n}"),
                at,
                at,
            )
        )
    return ticks


def _run(minutes, book=None, trades=(), strategy_cls=BookImbalance, config_cls=BookImbalanceConfig, **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(1000, USD)],
        base_currency=USD,
        book_type=BookType.L2_MBP,
    )
    engine.add_instrument(INSTRUMENT)
    if book is not None:
        engine.add_data([book])
    engine.add_data(_quotes(lambda t: MID, minutes))
    if trades:
        engine.add_data(list(trades))
    run_id = uuid.uuid7()
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    engine.add_actor(
        RecordingActor(
            env=Env.DEMO,
            run_id=run_id,
            instrument_id=INSTRUMENT_ID,
            record=records.append,
            blocks=blocks,
            maintenance_margin_rate=D("0.01"),
            min_liq_distance_pct=D(10),
            stale_quote_s=D(300),
            ledger=FundingLedger(),
        )
    )
    values = {
        "env": "demo",
        "run_id": str(run_id),
        "instrument_id": str(INSTRUMENT_ID),
        "use_market_context": False,
        "max_order_notional_usd": D(250),
        "max_leverage": D(2),
        "max_total_exposure_usd": D(500),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
    } | overrides
    strategy = strategy_cls(config_cls(**values), PauseGate(), blocks, records.append)
    fired: list[int] = []
    rule_on_firing = strategy.rule.on_firing

    def spy(bucket, *args):
        fired.append(bucket)
        return rule_on_firing(bucket, *args)

    strategy.rule.on_firing = spy
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records], fired


def test_a_heavy_book_with_buying_flow_enters_long_on_the_second_aligned_firing():
    _, strategy, rows, fired = _run(2, book=_snapshot("3.0000", "1.0000"), trades=_buys(120))

    enters = _decisions(rows, Intent.ENTER)
    first = _ns(START) // BUCKET_NS
    assert fired[:3] == [first, first + 1, first + 2]  # the firing at k * 15 s closes bucket k - 1
    assert enters, "no enter decision"
    assert enters[0].side == OrderSide.BUY
    assert enters[0].inputs["imbalance"] == "0.5"  # decision inputs are stored as text
    assert enters[0].inputs["bid_depth"] == "15"
    assert enters[0].decided_ns == _ns(START) + 2 * BUCKET_NS  # two lopsided firings: 12:00:15 and 12:00:30
    assert strategy.bar_type not in strategy.cache.bar_types()


def test_an_empty_ask_side_runs_the_firings_but_enters_nothing():
    _, _, rows, fired = _run(2, book=_snapshot("3.0000", None), trades=_buys(120))

    assert len(fired) >= 7
    assert _decisions(rows, Intent.ENTER) == []
