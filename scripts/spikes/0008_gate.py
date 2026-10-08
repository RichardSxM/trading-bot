"""Spec 0008 Milestone 1 gate spike. Throwaway: never merged as product code.

Checks the NautilusTrader 1.231.0 backtest engine against what spec 0008 assumes (AC-1, AC-5, AC-9, AC-14, AC-16):
  speed    one or two real own recorder hours through the AC-5 venue: events per second, wall time, peak RSS
  queue    a post only bid waits for the size ahead; a shrinking level and a trade plus its delta do not
           change that; liquidity consumption for taker orders
  latency  a 25 ms base latency delays an order by exactly 25 ms, and it fills against the book of that moment
  fees     commission on a 1,000 USD taker fill; quotes in the stream reach subscribers and MID INTERNAL bars
  tardis   one Tardis day: instrument id, size unit, snapshots, local_timestamp order, ticks, funding fields

Run (trader image, the 6 GB limit the lab gets):
  docker run --rm --memory 6g -v "$PWD/scripts/spikes:/spike" -v "$PWD/catalog-lab:/catalog-lab:ro" \
    -v "$PWD/tardis:/tardis:ro" kfs/trader:dev python /spike/0008_gate.py <check> [args]
"""

import gzip
import heapq
import itertools
import os
import resource
import sys
import time
from collections.abc import Iterator
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import LatencyModel, MakerTakerFeeModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import (
    BarType,
    BookOrder,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    BookAction,
    BookType,
    OmsType,
    OrderSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, TradeId, TraderId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer
from nautilus_trader.trading.strategy import Strategy

OWN = Path("/catalog-lab/own")
TARDIS = Path("/tardis")
INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
VENUE = Venue("KRAKEN")
F_LAST = 128
F_SNAPSHOT = 32
MS = 1_000_000
SEC = 1_000_000_000
HOUR = 3600 * SEC
LATENCY_NS = 25 * MS
RESULTS: dict[str, str] = {}
LOG_LEVEL = os.environ.get("SPIKE_LOG", "ERROR")


def result(name: str, ok: bool, detail: str) -> None:
    RESULTS[name] = f"{'PASS' if ok else 'FAIL'}: {detail}"
    print(f"[{name}] {RESULTS[name]}", flush=True)


def info(name: str, detail: str) -> None:
    RESULTS[name] = f"INFO: {detail}"
    print(f"[{name}] INFO: {detail}", flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ---------------------------------------------------------------- instrument and venue


def hour_file(table: str, hour: str) -> Path:
    name = f"2026-10-08T{hour}-00-00-000000000Z_2026-10-08T{hour}-59-59-999999999Z.parquet"
    return OWN / "data" / table / "PF_XBTUSD.KRAKEN" / name


def pinned_instrument() -> CryptoPerpetual:
    table = pq.read_table(hour_file("crypto_perpetual", "17"))
    return ArrowSerializer.deserialize(CryptoPerpetual, table)[0]


def with_fees(instrument: CryptoPerpetual, maker: str, taker: str) -> CryptoPerpetual:
    d = CryptoPerpetual.to_dict(instrument)
    d["maker_fee"] = maker
    d["taker_fee"] = taker
    return CryptoPerpetual.from_dict(d)


def make_engine(
    instrument: CryptoPerpetual,
    *,
    queue_position: bool = True,
    latency_ns: int = LATENCY_NS,
    log_level: str = LOG_LEVEL,
) -> BacktestEngine:
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-LAB"),
            logging=LoggingConfig(log_level=log_level, bypass_logging=log_level == "OFF"),
        )
    )
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USD)],
        base_currency=USD,
        default_leverage=Decimal(2),
        fee_model=MakerTakerFeeModel(),
        latency_model=LatencyModel(
            base_latency_nanos=latency_ns,
            insert_latency_nanos=0,
            update_latency_nanos=0,
            cancel_latency_nanos=0,
        ),
        book_type=BookType.L2_MBP,
        queue_position=queue_position,
        liquidity_consumption=True,
        trade_execution=True,
        bar_execution=False,
        use_reduce_only=True,
    )
    engine.add_instrument(instrument)
    return engine


# ---------------------------------------------------------------- the hour stream (AC-4, AC-5, AC-16)


def read_deltas(path: Path) -> Iterator[OrderBookDelta]:
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    for batch in pf.iter_batches(batch_size=65_536):
        table = pa.Table.from_batches([batch]).replace_schema_metadata(schema.metadata)
        yield from OrderBookDelta.from_pyo3_list(ArrowSerializer.deserialize(OrderBookDelta, table))


def read_trades(path: Path) -> list[TradeTick]:
    return TradeTick.from_pyo3_list(ArrowSerializer.deserialize(TradeTick, pq.read_table(path)))


def batches(deltas: Iterator[OrderBookDelta], *, drop_first_snapshot: bool) -> Iterator[OrderBookDeltas]:
    """Group deltas into batches of one feed message; optionally drop the hour start snapshot block.

    The recorder writes flags 0 on every feed delta (only the compactor's hour start snapshot carries F_LAST),
    and each message has its own ts_init, so a batch ends at F_LAST or when ts_init changes.
    """
    pending: list[OrderBookDelta] = []
    dropping = drop_first_snapshot
    for delta in deltas:
        if dropping:
            if delta.flags & F_SNAPSHOT:
                if delta.flags & F_LAST:
                    dropping = False
                continue
            dropping = False
        if pending and delta.ts_init != pending[-1].ts_init:
            yield OrderBookDeltas(INSTRUMENT_ID, pending)
            pending = []
        pending.append(delta)
        if delta.flags & F_LAST:
            yield OrderBookDeltas(INSTRUMENT_ID, pending)
            pending = []
    if pending:
        yield OrderBookDeltas(INSTRUMENT_ID, pending)


def merged(hours: list[str], instrument: CryptoPerpetual, chunk: int = 20_000) -> Iterator[list]:
    """One segment: the given back to back hours, inner hour snapshots dropped, trades merged by ts_init."""

    # one book across the hours, so the quote stream stays continuous at the hour boundary
    book = OrderBook(INSTRUMENT_ID, BookType.L2_MBP)
    top: list[tuple[int, int, int, int] | None] = [None]

    def book_events() -> Iterator[OrderBookDeltas | QuoteTick]:
        for i, hour in enumerate(hours):
            for batch in batches(read_deltas(hour_file("order_book_deltas", hour)), drop_first_snapshot=i > 0):
                book.apply_deltas(batch)
                yield batch
                bid, ask = book.best_bid_price(), book.best_ask_price()
                if bid is None or ask is None:
                    continue
                bsz, asz = book.best_bid_size(), book.best_ask_size()
                now = (bid.raw, ask.raw, bsz.raw, asz.raw)
                if now != top[0]:
                    top[0] = now
                    yield QuoteTick(INSTRUMENT_ID, bid, ask, bsz, asz, batch.ts_init, batch.ts_init)

    def trade_events() -> Iterator[TradeTick]:
        for hour in hours:
            yield from read_trades(hour_file("trade_tick", hour))

    # book events sort before trades at the same ts_init (rank 0 vs 1), stable inside each source
    keyed_book = ((e.ts_init, 0, n, e) for n, e in enumerate(book_events()))
    keyed_trades = ((e.ts_init, 1, n, e) for n, e in enumerate(trade_events()))
    out: list = []
    for _, _, _, event in heapq.merge(keyed_book, keyed_trades):
        out.append(event)
        if len(out) >= chunk:
            yield out
            out = []
    if out:
        yield out


# ---------------------------------------------------------------- strategies


class TakerConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    interval_s: int = 60
    qty: str = "0.0100"


class PeriodicTaker(Strategy):
    """Every interval_s of quote time: flat → market entry (alternating side); long or short → market exit."""

    def __init__(self, config: TakerConfig) -> None:
        super().__init__(config)
        self.next_ns = 0
        self.side = OrderSide.BUY
        self.quotes = 0
        self.fills: list[OrderFilled] = []

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.quotes += 1
        if tick.ts_init < self.next_ns:
            return
        self.next_ns = tick.ts_init + self.config.interval_s * SEC
        if self.portfolio.is_flat(self.config.instrument_id):
            side = self.side
            self.side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            order = self.order_factory.market(self.config.instrument_id, side, Quantity.from_str(self.config.qty))
        else:
            net = self.portfolio.net_position(self.config.instrument_id)
            side = OrderSide.SELL if net > 0 else OrderSide.BUY
            order = self.order_factory.market(
                self.config.instrument_id, side, Quantity.from_str(self.config.qty), reduce_only=True
            )
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        self.fills.append(event)


class ScriptConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class Scripted(Strategy):
    """Runs actions keyed by quote count or ts; records every order event it sees."""

    def __init__(self, config: ScriptConfig) -> None:
        super().__init__(config)
        self.actions: list = []  # (ts_init_at_or_after, callable)
        self.events: list = []
        self.quotes: list[QuoteTick] = []
        self.bars: list = []
        self.bar_type: BarType | None = None

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self.config.instrument_id)
        if self.bar_type is not None:
            self.subscribe_bars(self.bar_type)
        for n, (ts, act) in enumerate(self.actions):
            self.clock.set_time_alert_ns(f"act{n}", ts, lambda _e, act=act: act(self))

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.quotes.append(tick)

    def on_bar(self, bar) -> None:
        self.bars.append(bar)

    def on_event(self, event) -> None:
        self.events.append((self.clock.timestamp_ns(), event))


# ---------------------------------------------------------------- synthetic data helpers


def px(v: int | str) -> Price:
    return Price.from_str(str(v))


def qty(v: str) -> Quantity:
    return Quantity.from_str(v)


def snapshot(ts: int, bids: list[tuple[int, str]], asks: list[tuple[int, str]]) -> OrderBookDeltas:
    rows = [OrderBookDelta.clear(INSTRUMENT_ID, 0, ts, ts)]
    for side, levels in ((OrderSide.BUY, bids), (OrderSide.SELL, asks)):
        for p, s in levels:
            rows.append(
                OrderBookDelta(INSTRUMENT_ID, BookAction.ADD, BookOrder(side, px(p), qty(s), 0), F_SNAPSHOT, 0, ts, ts)
            )
    last = rows[-1]
    rows[-1] = OrderBookDelta(INSTRUMENT_ID, last.action, last.order, F_SNAPSHOT | F_LAST, 0, ts, ts)
    return OrderBookDeltas(INSTRUMENT_ID, rows)


def update(ts: int, side: OrderSide, p: int, s: str, action: BookAction = BookAction.UPDATE) -> OrderBookDeltas:
    return OrderBookDeltas(
        INSTRUMENT_ID,
        [OrderBookDelta(INSTRUMENT_ID, action, BookOrder(side, px(p), qty(s), 0), F_LAST, 0, ts, ts)],
    )


def quote(ts: int, bid: int, ask: int, bsz: str, asz: str) -> QuoteTick:
    return QuoteTick(INSTRUMENT_ID, px(bid), px(ask), qty(bsz), qty(asz), ts, ts)


_trade_n = [0]


def trade(ts: int, p: int, s: str, aggressor: AggressorSide) -> TradeTick:
    _trade_n[0] += 1
    return TradeTick(INSTRUMENT_ID, px(p), qty(s), aggressor, TradeId(f"T{_trade_n[0]}"), ts, ts)


def run_script(
    data: list, actions: list, *, queue_position: bool = True, latency_ns: int = LATENCY_NS, flush: bool = True
) -> Scripted:
    instrument = with_fees(pinned_instrument(), "0.0002", "0.0005")
    engine = make_engine(instrument, queue_position=queue_position, latency_ns=latency_ns)
    strategy = Scripted(ScriptConfig(instrument_id=INSTRUMENT_ID))
    strategy.actions = sorted(actions, key=lambda a: a[0])
    engine.add_strategy(strategy)
    # In flight orders are processed at the first data event at or after their arrival time, so synthetic
    # scenarios get a 1 ms heartbeat of quotes (an L2_MBP venue never applies quotes to its book), like a
    # dense real feed, plus one trailing quote that flushes whatever is still in flight.
    first = min(d.ts_init for d in data)
    last = max(d.ts_init for d in data)
    beats = [quote(ts, 59000, 61000, "1.0000", "1.0000") for ts in range(first + MS, last + SEC, MS)]
    engine.add_data([*data, *beats] if flush else data, sort=True)
    engine.run()
    engine.dispose()
    return strategy


def fills(s: Scripted) -> list[tuple[int, OrderFilled]]:
    return [(t, e) for t, e in s.events if isinstance(e, OrderFilled)]


def post_only_bid(price: int, size: str):
    def act(s: Scripted) -> None:
        s.submit_order(
            s.order_factory.limit(
                INSTRUMENT_ID, OrderSide.BUY, qty(size), px(price), post_only=True, time_in_force=TimeInForce.GTC
            )
        )

    return act


def market(side: OrderSide, size: str):
    def act(s: Scripted) -> None:
        s.submit_order(s.order_factory.market(INSTRUMENT_ID, side, qty(size)))

    return act


# ---------------------------------------------------------------- checks


def check_speed(hours: list[str]) -> None:
    instrument = with_fees(pinned_instrument(), "0.0002", "0.0005")
    engine = make_engine(instrument)
    strategy = PeriodicTaker(TakerConfig(instrument_id=INSTRUMENT_ID))
    engine.add_strategy(strategy)
    counts = {"events": 0}

    def counted() -> Iterator[list]:
        for chunk in merged(hours, instrument):
            counts["events"] += len(chunk)
            yield chunk

    engine.add_data_iterator("own_hours", counted())
    start = time.monotonic()
    seg_start = 1_791_478_800 * SEC + (int(hours[0]) - 17) * HOUR
    engine.run(start=seg_start, end=seg_start + len(hours) * HOUR - 1)
    wall = time.monotonic() - start
    peak = rss_mb()
    n = counts["events"]
    rate = n / wall if wall else 0
    per_hour = wall / len(hours)
    day_min = per_hour * 24 / 60
    info(
        "speed",
        f"hours={hours} events={n} wall={wall:.0f}s rate={rate:,.0f}/s quotes_seen={strategy.quotes} "
        f"fills={len(strategy.fills)} peak_rss={peak:.0f}MB day_estimate={day_min:.1f}min",
    )
    result("speed_day_under_30min", day_min < 30, f"{day_min:.1f} min per 24 h at this hour's density")
    result("speed_rss_under_6g", peak < 6 * 1024, f"peak RSS {peak:.0f} MB")
    if strategy.fills:
        f = strategy.fills[0]
        info("speed_first_fill", f"{f.order_side} {f.last_qty} @ {f.last_px} {f.liquidity_side} fee {f.commission}")
    engine.dispose()


def check_queue() -> None:
    t0 = 1_791_478_800 * SEC  # 2026-10-08 17:00 UTC
    book = [snapshot(t0, [(60000, "5.0000"), (59999, "5.0000")], [(60001, "5.0000"), (60002, "5.0000")])]
    q0 = [quote(t0, 60000, 60001, "5.0000", "5.0000")]
    sell = AggressorSide.SELLER
    order_at = t0 + 1 * MS  # arrives at t0 + 26 ms

    def first_fill_after(trades: list, *, queue_position: bool = True, extra: list | None = None):
        s = run_script(
            book + q0 + trades + (extra or []),
            [(order_at, post_only_bid(60000, "1.0000"))],
            queue_position=queue_position,
        )
        f = fills(s)
        return s, f

    trades = [trade(t0 + (100 + i) * MS, 60000, "1.0000", sell) for i in range(6)]
    _, f = first_fill_after(trades)
    first = (f[0][1].ts_event - t0) // MS if f else None
    result(
        "queue_waits_for_5_ahead",
        first == 105,
        f"first fill at t0+{first} ms (trades at t0+100..105 ms, 1 BTC each; 5 ahead → expect the 6th, +105)",
    )

    _, f = first_fill_after(trades, queue_position=False)
    first = (f[0][1].ts_event - t0) // MS if f else None
    result("queue_optimistic_fills_first_trade", first == 100, f"queue_position=False: first fill at t0+{first} ms")

    # level shrinks 5 → 1 (cancels ahead) before the trades: pessimistic means no credit
    shrink = [update(t0 + 50 * MS, OrderSide.BUY, 60000, "2.0000")]  # 1 ahead + our 1 is not in data; level 2
    _, f = first_fill_after(trades, extra=shrink)
    first = (f[0][1].ts_event - t0) // MS if f else None
    result(
        "queue_shrink_gives_no_credit",
        first == 105,
        f"after an UPDATE 5→2 at the level, first fill at t0+{first} ms (105 = no credit for cancels ahead)",
    )

    # level DELETE then re-ADD: the engine puts the order at the front
    delete = [
        update(t0 + 50 * MS, OrderSide.BUY, 60000, "0.0000", BookAction.DELETE),
        update(t0 + 60 * MS, OrderSide.BUY, 60000, "3.0000", BookAction.ADD),
    ]
    _, f = first_fill_after(trades, extra=delete)
    first = (f[0][1].ts_event - t0) // MS if f else None
    info("queue_delete_resets", f"after DELETE+ADD of the level, first fill at t0+{first} ms (100 = moved to front)")

    # trade then the delta that reflects it: the delta must not decrement the queue a second time
    mixed = []
    size = 5
    for i in range(6):
        ts = t0 + (100 + 2 * i) * MS
        mixed.append(trade(ts, 60000, "1.0000", sell))
        size = max(size - 1, 1)
        mixed.append(update(ts + 1 * MS, OrderSide.BUY, 60000, f"{size}.0000"))
    _, f = first_fill_after(mixed)
    first = (f[0][1].ts_event - t0) // MS if f else None
    result(
        "queue_trade_and_delta_count_once",
        first == 110,
        f"trade then matching delta, 6 times: first fill at t0+{first} ms (110 = the 6th trade, counted once)",
    )

    # excess: 5 ahead, one 6 BTC trade, our order 2 BTC → fills 1 BTC only from that print
    big = [trade(t0 + 100 * MS, 60000, "6.0000", sell)]
    s = run_script(book + q0 + big, [(order_at, post_only_bid(60000, "2.0000"))])
    got = sum((e.last_qty.as_decimal() for _, e in fills(s)), Decimal(0))
    result("queue_excess_only", got == Decimal(1), f"5 ahead, one 6 BTC print, 2 BTC order: filled {got} BTC")

    # taker liquidity consumption: two 3 BTC market buys against 5 + 5 on the ask, no delta in between
    s = run_script(
        book + q0 + [quote(t0 + 200 * MS, 60000, 60001, "5.0000", "5.0000")],
        [(t0 + 1 * MS, market(OrderSide.BUY, "3.0000")), (t0 + 2 * MS, market(OrderSide.BUY, "3.0000"))],
    )
    got = [(str(e.last_px), str(e.last_qty)) for _, e in fills(s)]
    result(
        "taker_consumes_liquidity",
        got == [("60001", "3.0000"), ("60001", "2.0000"), ("60002", "1.0000")],
        f"fills {got} (expect the second order to walk to 60002)",
    )


def check_latency() -> None:
    t0 = 1_791_478_800 * SEC
    book = [snapshot(t0, [(60000, "5.0000")], [(60001, "5.0000")])]
    data = [
        *book,
        quote(t0 + 1 * MS, 60000, 60001, "5.0000", "5.0000"),
        update(t0 + 11 * MS, OrderSide.SELL, 60001, "0.0000", BookAction.DELETE),
        update(t0 + 11 * MS + 1, OrderSide.SELL, 60005, "5.0000", BookAction.ADD),
        update(t0 + 26 * MS + 1, OrderSide.SELL, 60005, "0.0000", BookAction.DELETE),
        update(t0 + 26 * MS + 2, OrderSide.SELL, 60010, "5.0000", BookAction.ADD),
        quote(t0 + 40 * MS, 60000, 60010, "5.0000", "5.0000"),
    ]
    s = run_script(data, [(t0 + 1 * MS, market(OrderSide.BUY, "1.0000"))])
    f = fills(s)
    if not f:
        result("latency_25ms", False, "no fill")
        return
    e = f[0][1]
    delay = (e.ts_event - (t0 + 1 * MS)) / MS
    info("latency_fill", f"submitted t0+1 ms, filled ts_event t0+{(e.ts_event - t0) / MS:.6f} ms at {e.last_px}")
    result(
        "latency_25ms",
        delay == 25 and str(e.last_px) == "60005",
        f"delay {delay} ms, price {e.last_px} (expect 25 ms and 60005, the book at t+25 ms; 60001 = book at t, "
        f"60010 = processed late)",
    )

    # no data after arrival time: is the order processed at all? (AC-8 final batch)
    data2 = [*book, quote(t0 + 1 * MS, 60000, 60001, "5.0000", "5.0000")]
    s = run_script(data2, [(t0 + 1 * MS, market(OrderSide.BUY, "1.0000"))], flush=False)
    info(
        "latency_no_later_data",
        f"with no data after submit: {len(fills(s))} fill(s) (0 means AC-8's final batch is needed)",
    )


def check_fees() -> None:
    instrument = with_fees(pinned_instrument(), "0.0002", "0.0005")
    engine = make_engine(instrument)
    strategy = Scripted(ScriptConfig(instrument_id=INSTRUMENT_ID))
    strategy.bar_type = BarType.from_str("PF_XBTUSD.KRAKEN-1-MINUTE-MID-INTERNAL")
    state: dict = {}

    def buy_1000(s: Scripted) -> None:
        q = s.quotes[-1]
        mid = (q.bid_price.as_decimal() + q.ask_price.as_decimal()) / 2
        size = (Decimal(1000) / mid).quantize(Decimal("0.0001"), rounding="ROUND_FLOOR")
        state["size"] = size
        s.submit_order(s.order_factory.market(INSTRUMENT_ID, OrderSide.BUY, Quantity(size, 4)))

    def sell_back(s: Scripted) -> None:
        s.submit_order(
            s.order_factory.market(INSTRUMENT_ID, OrderSide.SELL, Quantity(state["size"], 4), reduce_only=True)
        )

    t0 = 1_791_478_800 * SEC
    strategy.actions = [(t0 + 60 * SEC, buy_1000), (t0 + 120 * SEC, sell_back)]
    engine.add_strategy(strategy)

    def ten_minutes() -> Iterator[list]:
        for chunk in merged(["17"], instrument):
            if chunk[0].ts_init > t0 + 600 * SEC:
                return
            yield chunk

    engine.add_data_iterator("own_10min", ten_minutes())
    engine.run(start=t0, end=t0 + 600 * SEC)  # without start, time bars run from the 1970 clock origin
    f = fills(strategy)
    ok = True
    details = []
    for _, e in f:
        notional = e.last_px.as_decimal() * e.last_qty.as_decimal()
        exact = Decimal("0.0005") * notional
        cents = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        got = e.commission.as_decimal()
        ok = ok and got == cents and e.commission.currency == USD
        details.append(
            f"{e.order_side.name} {e.last_qty}@{e.last_px} {e.liquidity_side.name} fee {e.commission} "
            f"(exact {exact}, half even cents {cents})"
        )
    result("fees_taker_cents", ok and bool(f), "; ".join(details) or "no fills")
    result("quotes_reach_strategy", len(strategy.quotes) > 100, f"{len(strategy.quotes)} quotes in 10 min")
    stamps = [f"{(b.ts_event - t0) / SEC:.3f}s" for b in strategy.bars]
    # the aggregator also closes one bar at the run start itself, from the snapshot quote at that same ns
    full = [b for b in strategy.bars if (b.ts_event - t0) % (60 * SEC) == 0 and t0 < b.ts_event <= t0 + 600 * SEC]
    result(
        "quotes_build_mid_bars",
        len(full) == 10,
        f"{len(full)} full 1 min MID-INTERNAL bars in 10 min; all bar ts_event from 17:00 = {stamps}",
    )
    engine.dispose()


def check_tardis() -> None:
    instrument = pinned_instrument()
    book_csv = TARDIS / "cryptofacilities_incremental_book_L2_2026-10-01_PF_XBTUSD.csv.gz"
    trades_csv = TARDIS / "cryptofacilities_trades_2026-10-01_PF_XBTUSD.csv.gz"
    ticker_csv = TARDIS / "cryptofacilities_derivative_ticker_2026-10-01_PF_XBTUSD.csv.gz"
    from nautilus_trader.adapters.tardis.loaders import TardisCSVDataLoader

    loader = TardisCSVDataLoader(
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        instrument_id=INSTRUMENT_ID,
    )
    first: list[OrderBookDelta] = []
    for chunk in loader.stream_deltas(book_csv, chunk_size=50_000, limit=50_000):
        first.extend(chunk)
    d0 = first[0]
    snap = [d for d in first if d.flags & F_SNAPSHOT]
    first_plain = next((d.action.name for d in first if not d.flags & F_SNAPSHOT), None)
    ordered = all(d.ts_event <= d.ts_init for d in first)
    info(
        "tardis_deltas",
        f"id={d0.instrument_id} first action={d0.action.name} flags={d0.flags} "
        f"snapshot rows in first 50k={len(snap)} first non snapshot={first_plain} "
        f"sizes sample={[str(d.order.size) for d in first[1:4]]} ts_event<=ts_init={ordered}",
    )
    result("tardis_instrument_id", d0.instrument_id == INSTRUMENT_ID, str(d0.instrument_id))
    trades = next(loader.stream_trades(trades_csv, chunk_size=5, limit=5))
    info("tardis_trades", f"{[(str(t.price), str(t.size), t.aggressor_side.name) for t in trades]}")

    # whole day, streamed in batches (one read_csv of 45M rows does not fit): snapshot blocks,
    # local_timestamp order, exchange time after local time, tick multiples
    import numpy as np

    cols = ["timestamp", "local_timestamp", "is_snapshot", "price", "amount"]
    opts = pacsv.ConvertOptions(
        include_columns=cols, column_types={"price": pa.string(), "amount": pa.string(), "is_snapshot": pa.bool_()}
    )
    reader = pacsv.open_csv(book_csv, convert_options=opts, read_options=pacsv.ReadOptions(block_size=64 << 20))
    n = back_n = ahead_n = 0
    back_max = 0
    prev_local = None
    prev_snap = False
    starts: list[tuple[int, int]] = []  # (row, local_timestamp us) where a snapshot block begins
    prices: set[str] = set()
    sizes: set[str] = set()
    for rb in reader:
        local = rb.column("local_timestamp").to_numpy().astype(np.int64)
        exch = rb.column("timestamp").to_numpy().astype(np.int64)
        snap = rb.column("is_snapshot").to_numpy(zero_copy_only=False)
        seq = local if prev_local is None else np.concatenate(([prev_local], local))
        step = seq[:-1] - seq[1:]
        back_n += int((step > 0).sum())
        back_max = max(back_max, int(step.max()) if len(step) else 0)
        ahead_n += int((exch > local).sum())
        prev_s = np.concatenate(([prev_snap], snap[:-1]))
        for i in np.flatnonzero(snap & ~prev_s)[:50]:
            starts.append((n + int(i), int(local[i])))
        prices.update(rb.column("price").to_pylist())
        sizes.update(rb.column("amount").to_pylist())
        prev_local, prev_snap = int(local[-1]), bool(snap[-1])
        n += rb.num_rows
    info(
        "tardis_day",
        f"rows={n:,} local_timestamp backwards steps={back_n} max back={back_max} us "
        f"snapshot blocks={len(starts)} first at {starts[:3]} exchange>local rows={ahead_n}",
    )
    inc_p = instrument.price_increment.as_decimal()
    inc_s = instrument.size_increment.as_decimal()
    bad_p = [p for p in prices if Decimal(p) % inc_p]
    bad_s = [s for s in sizes if Decimal(s) % inc_s]
    result(
        "tardis_ticks",
        not bad_p and not bad_s,
        f"distinct prices {len(prices)}, off {inc_p}: {len(bad_p)} {bad_p[:3]}; distinct sizes {len(sizes)}, "
        f"off {inc_s}: {len(bad_s)} {bad_s[:3]}",
    )
    tt = pacsv.read_csv(
        trades_csv, convert_options=pacsv.ConvertOptions(column_types={"price": pa.string(), "amount": pa.string()})
    )
    bad_tp = [p for p in set(tt.column("price").to_pylist()) if Decimal(p) % inc_p]
    bad_ts = [s for s in set(tt.column("amount").to_pylist()) if Decimal(s) % inc_s]
    result(
        "tardis_trade_ticks",
        not bad_tp and not bad_ts,
        f"trade prices off: {len(bad_tp)} {bad_tp[:3]}; sizes off: {len(bad_ts)} {bad_ts[:3]}",
    )

    # funding
    with gzip.open(ticker_csv, "rt") as fh:
        header = fh.readline().strip()
        sample = [fh.readline().strip() for _ in range(2)]
    info("ticker_columns", header)
    info("ticker_sample", " | ".join(sample))
    tk = pacsv.read_csv(ticker_csv)
    fr = tk.column("funding_rate").to_numpy(zero_copy_only=False)
    fts = tk.column("funding_timestamp").to_numpy(zero_copy_only=False)
    mark = tk.column("mark_price").to_numpy(zero_copy_only=False)
    uniq_ts = sorted(set(int(x) for x in fts if x == x))
    steps = sorted({(b - a) // 1_000_000 for a, b in itertools.pairwise(uniq_ts)})
    changes = int((fr[1:] != fr[:-1]).sum())
    info(
        "ticker_funding",
        f"rows={tk.num_rows} funding_rate min={np.nanmin(fr):.3e} max={np.nanmax(fr):.3e} "
        f"rate changes={changes} funding_timestamp values={len(uniq_ts)} steps(s)={steps} "
        f"mark sample={mark[0]} rate*mark sample={fr[0] * mark[0]:.4f}",
    )
    rates = []
    for chunk in loader.stream_funding_rates(ticker_csv, chunk_size=100_000):
        rates.extend(chunk)
    r0 = rates[0]
    info(
        "loader_funding",
        f"FundingRateUpdate count={len(rates)} first rate={r0.rate} next_funding_ns={r0.next_funding_ns} "
        f"ts_event={r0.ts_event} ts_init={r0.ts_init} distinct rates={len({str(r.rate) for r in rates})}",
    )


def main() -> int:
    check = sys.argv[1] if len(sys.argv) > 1 else "all"
    if check == "speed":
        check_speed(sys.argv[2:] or ["17"])
    elif check == "queue":
        check_queue()
    elif check == "latency":
        check_latency()
    elif check == "fees":
        check_fees()
    elif check == "tardis":
        check_tardis()
    else:
        print(f"unknown check {check!r}")
        return 64
    print("\n== results ==")
    for name, line in RESULTS.items():
        print(f"{name:40s} {line}")
    return 1 if any(v.startswith("FAIL") for v in RESULTS.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
