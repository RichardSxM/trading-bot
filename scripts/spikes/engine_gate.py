"""Spec 0014 gate G-3, imported by 0014_gate.py inside the trader image. Throwaway.

The AC-8 venue fed the AC-9 path: per minute four points, each a NO_AGGRESSOR trade then a quote
with the slippage spread. Checks timer order, market, stop and limit fills, zero size trades,
internal hour bars, the position flag lag, and throughput.
"""

import os
import resource
import time
from collections.abc import Callable, Iterator
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, BookType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled, PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId, TradeId, TraderId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
VENUE = Venue("KRAKEN")
MS = 1_000_000
SEC = 1_000_000_000
MIN = 60 * SEC
HOUR = 60 * MIN
T0 = 1_790_812_800 * SEC  # 2026-10-01 00:00 UTC
SLIP = Decimal("0.0002")  # 2 bps
QUOTE_SIZE = Quantity.from_str("1000.0000")
STEP = Decimal("0.0001")
LOG_LEVEL = os.environ.get("SPIKE_LOG", "ERROR")
RESULTS: dict[str, str] = {}


def result(name: str, ok: bool, detail: str) -> None:
    RESULTS[name] = f"{'PASS' if ok else 'FAIL'}: {detail}"
    print(f"[{name}] {RESULTS[name]}", flush=True)


def info(name: str, detail: str) -> None:
    RESULTS[name] = f"INFO: {detail}"
    print(f"[{name}] INFO: {detail}", flush=True)


def instrument() -> CryptoPerpetual:
    import json

    with open("/spike/PF_XBTUSD.KRAKEN.json") as fh:  # copied from apps/lab instruments
        raw = json.load(fh)
    raw.pop("_note", None)
    raw["maker_fee"], raw["taker_fee"] = "0.0002", "0.0005"
    return CryptoPerpetual.from_dict(raw)


def make_engine() -> BacktestEngine:
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-LAB"),
            logging=LoggingConfig(log_level=LOG_LEVEL, bypass_logging=LOG_LEVEL == "OFF"),
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
        book_type=BookType.L1_MBP,
        queue_position=False,
        liquidity_consumption=False,
        trade_execution=False,
        bar_execution=False,
        use_reduce_only=True,
    )
    engine.add_instrument(instrument())
    return engine


# ---------------------------------------------------------------- the path (AC-9)


class SideFlag:
    """What PositionSideFlag would hold: +1 long, -1 short, 0 flat, and when it last changed."""

    def __init__(self) -> None:
        self.side = 0
        self.changed_ns = 0


def points(m: int, o: int, h: int, lo: int, c: int, side: int) -> list[tuple[int, int]]:
    if side > 0:
        first, second = lo, h
    elif side < 0:
        first, second = h, lo
    else:
        first, second = (h, lo) if (h - o) <= (o - lo) else (lo, h)
    return [(m + MS, o), (m + 20 * SEC, first), (m + 40 * SEC, second), (m + 60 * SEC - MS, c)]


_n = [0]


def path_events(m: int, candle: tuple[int, int, int, int], side: int, vol: Decimal = Decimal("1")) -> list:
    out: list = []
    quarter = max(STEP, (vol / 4).quantize(STEP, rounding=ROUND_FLOOR))
    for i, (ts, p) in enumerate(points(m, *candle, side)):
        size = quarter if i < 3 else max(STEP, vol - 3 * quarter)
        _n[0] += 1
        out.append(
            TradeTick(
                INSTRUMENT_ID,
                Price(p, 0),
                Quantity.from_str(str(size)),
                AggressorSide.NO_AGGRESSOR,
                TradeId(f"{ts}-{i}"),
                ts,
                ts,
            )
        )
        bid = (Decimal(p) * (1 - SLIP)).to_integral_value(rounding=ROUND_FLOOR)
        ask = (Decimal(p) * (1 + SLIP)).to_integral_value(rounding=ROUND_CEILING)
        out.append(QuoteTick(INSTRUMENT_ID, Price(int(bid), 0), Price(int(ask), 0), QUOTE_SIZE, QUOTE_SIZE, ts, ts))
    return out


def bid_ask(p: int) -> tuple[int, int]:
    return (
        int((Decimal(p) * (1 - SLIP)).to_integral_value(rounding=ROUND_FLOOR)),
        int((Decimal(p) * (1 + SLIP)).to_integral_value(rounding=ROUND_CEILING)),
    )


def stream(
    candles: dict[int, tuple[int, int, int, int]], flag: SideFlag, log: list, final: bool = True
) -> Iterator[list]:
    """One minute per chunk; the side is read when the minute is generated."""
    last = None
    for m in sorted(candles):
        log.append((m, flag.side, flag.changed_ns))
        chunk = path_events(m, candles[m], flag.side)
        last = (m, candles[m][3])
        yield chunk
    if final and last:
        m, c = last
        ts = m + 60 * SEC - MS + 2 * MS
        b, a = bid_ask(c)
        yield [QuoteTick(INSTRUMENT_ID, Price(b, 0), Price(a, 0), QUOTE_SIZE, QUOTE_SIZE, ts, ts)]


class FlagConfig(ActorConfig, frozen=True):
    pass


class PositionSideFlag(Actor):
    def __init__(self, flag: SideFlag) -> None:
        super().__init__(FlagConfig())
        self.flag = flag

    def on_start(self) -> None:
        self.subscribe_quote_ticks(INSTRUMENT_ID)  # only to see time move
        # A plain Actor gets no position events by default; subscribe to the topic.
        self.msgbus.subscribe(topic="events.position.*", handler=self.on_position)

    def on_position(self, event) -> None:
        if isinstance(event, (PositionOpened, PositionChanged, PositionClosed)):
            q = event.signed_qty
            self.flag.side = 0 if q == 0 else (1 if q > 0 else -1)
            self.flag.changed_ns = event.ts_event


class ScriptConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId


class Scripted(Strategy):
    def __init__(self, config: ScriptConfig) -> None:
        super().__init__(config)
        self.alerts: list[tuple[int, Callable]] = []
        self.on_bar_do: Callable | None = None
        self.bar_type: BarType | None = None
        self.events: list = []
        self.bars: list[Bar] = []
        self.order: list[tuple[str, int]] = []

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self.config.instrument_id)
        self.subscribe_trade_ticks(self.config.instrument_id)
        if self.bar_type is not None:
            self.subscribe_bars(self.bar_type)
        for n, (ts, act) in enumerate(self.alerts):
            self.clock.set_time_alert_ns(
                f"a{n}", ts, lambda e, act=act: (self.order.append(("timer", e.ts_event)), act(self))
            )

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.order.append(("trade", tick.ts_init))

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.order.append(("quote", tick.ts_init))

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        self.order.append(("bar", bar.ts_event))
        if self.on_bar_do:
            self.on_bar_do(self, bar)

    def on_event(self, event) -> None:
        self.events.append((self.clock.timestamp_ns(), event))


def fills(s: Scripted) -> list[OrderFilled]:
    return [e for _, e in s.events if isinstance(e, OrderFilled)]


def run(candles, *, alerts=(), bar_spec: str | None = None, on_bar=None, start=None, end=None, flag_log=None):
    engine = make_engine()
    flag = SideFlag()
    actor = PositionSideFlag(flag)
    s = Scripted(ScriptConfig(instrument_id=INSTRUMENT_ID))
    s.alerts = list(alerts)
    s.on_bar_do = on_bar
    if bar_spec:
        s.bar_type = BarType.from_str(f"{INSTRUMENT_ID}-{bar_spec}-INTERNAL")
    engine.add_actor(actor)
    engine.add_strategy(s)
    log = flag_log if flag_log is not None else []
    engine.add_data_iterator("candles", stream(candles, flag, log))
    first = min(candles)
    engine.run(start=start if start is not None else first, end=end if end is not None else max(candles) + 2 * MIN)
    engine.dispose()
    return s


def flat_minutes(start: int, n: int, price: int = 60_000, wiggle: int = 10) -> dict[int, tuple[int, int, int, int]]:
    return {start + i * MIN: (price, price + wiggle, price - wiggle, price) for i in range(n)}


# ---------------------------------------------------------------- checks


def check_timer_order() -> None:
    candles = flat_minutes(T0, 3)
    s = run(candles, alerts=[(T0 + MIN, lambda s: None)])
    seq = [k for k, ts in s.order if T0 + MIN <= ts <= T0 + MIN + 5 * MS]
    ok = seq[:1] == ["timer"] and "trade" in seq
    result("timer_before_data", ok, f"order around T: {seq[:4]} (timer at T, next minute's open point at T + 1 ms)")


def check_market_on_bar_close() -> None:
    # Two hours plus; buy on the first 1-HOUR bar close.
    candles = flat_minutes(T0, 130)
    last_min = T0 + 59 * MIN
    candles[last_min] = (60_000, 60_050, 59_950, 60_020)  # hour close 60020

    def on_bar(s, bar):
        if not fills(s) and len(s.bars) == 1:
            s.submit_order(s.order_factory.market(INSTRUMENT_ID, OrderSide.BUY, Quantity.from_str("0.1000")))

    s = run(candles, bar_spec="1-HOUR-LAST", on_bar=on_bar)
    f = fills(s)
    _, ask = bid_ask(60_020)
    ok = bool(f) and int(f[0].last_px) == ask and s.bars[0].close == Price(60_020, 0)
    result(
        "market_at_close_ask",
        ok,
        f"bar close {s.bars[0].close if s.bars else None} at {s.bars[0].ts_event if s.bars else None}, "
        f"fill {f[0].last_px if f else None} "
        f"at {f[0].ts_event if f else None}, expected ask {ask}; liquidity {f[0].liquidity_side if f else None}",
    )


def check_stop_fill() -> None:
    candles = flat_minutes(T0, 10)
    candles[T0 + 5 * MIN] = (60_000, 60_010, 59_000, 59_100)  # crash through 59_500

    def enter(s):
        s.submit_order(s.order_factory.market(INSTRUMENT_ID, OrderSide.BUY, Quantity.from_str("0.1000")))

    def stop(s):
        s.submit_order(
            s.order_factory.stop_market(
                INSTRUMENT_ID, OrderSide.SELL, Quantity.from_str("0.1000"), Price(59_500, 0), reduce_only=True
            )
        )

    s = run(candles, alerts=[(T0 + 2 * MIN, enter), (T0 + 3 * MIN, stop)])
    f = fills(s)
    exp_bid, _ = bid_ask(59_000)
    got = [(int(x.last_px), x.order_side.name, x.ts_event) for x in f]
    ok = len(f) == 2 and int(f[1].last_px) == exp_bid
    result("stop_fills_at_quote", ok, f"fills {got}; expected stop fill at the low's bid {exp_bid} (trigger 59500)")


def check_limit_trade_through() -> None:
    # Touch: low = 59_500 exactly -> ask 59_512 > 59_500, must not fill. Through: low 59_480 -> ask 59_492 <= 59_500.
    touch = flat_minutes(T0, 10)
    touch[T0 + 5 * MIN] = (60_000, 60_010, 59_500, 59_900)
    through = flat_minutes(T0, 10)
    through[T0 + 5 * MIN] = (60_000, 60_010, 59_480, 59_900)

    def bid(s):
        s.submit_order(
            s.order_factory.limit(
                INSTRUMENT_ID,
                OrderSide.BUY,
                Quantity.from_str("0.1000"),
                Price(59_500, 0),
                time_in_force=TimeInForce.GTC,
            )
        )

    a = fills(run(touch, alerts=[(T0 + 2 * MIN, bid)]))
    b = fills(run(through, alerts=[(T0 + 2 * MIN, bid)]))
    ok = not a and len(b) == 1 and int(b[0].last_px) == 59_500 and b[0].liquidity_side.name == "MAKER"
    result(
        "limit_trade_through_only",
        ok,
        f"touch fills {[(int(x.last_px)) for x in a]}; through fills "
        f"{[(int(x.last_px), x.liquidity_side.name) for x in b]}",
    )


def check_zero_trade() -> None:
    try:
        TradeTick(
            INSTRUMENT_ID, Price(1, 0), Quantity.from_str("0.0000"), AggressorSide.NO_AGGRESSOR, TradeId("z"), 1, 1
        )
        result("zero_size_trade_refused", False, "a zero size TradeTick was accepted")
    except Exception as exc:
        result("zero_size_trade_refused", True, f"{type(exc).__name__}: {exc}")


def check_internal_bars() -> None:
    # Start at 00:30 (partial first hour), skip hour 02:00-03:00 entirely, run to 04:00.
    start = T0 + 30 * MIN
    candles = {k: v for k, v in flat_minutes(start, 210).items() if not (T0 + 2 * HOUR <= k < T0 + 3 * HOUR)}
    s = run(candles, bar_spec="1-HOUR-LAST", start=start)
    rows = [((b.ts_event - T0) // MIN, int(b.open), int(b.close), str(b.volume)) for b in s.bars]
    info("internal_hour_bars", f"bars (minutes from 00:00, open, close, volume): {rows}")


def s_flag_changed(log):
    return next((c for _, side, c in log if side), None)


def check_flag_lag() -> None:
    candles = flat_minutes(T0, 10)
    log: list = []

    def enter(s):
        s.submit_order(s.order_factory.market(INSTRUMENT_ID, OrderSide.BUY, Quantity.from_str("0.1000")))

    s = run(candles, alerts=[(T0 + 3 * MIN + 30 * SEC, enter)], flag_log=log)
    fill_ts = fills(s)[0].ts_event
    gen = [(((m - T0) // MIN), side) for m, side, _ in log]
    first_long = next((m for m, side, _ in log if side > 0), None)
    # The fill is mid minute 3; the next minute generated after it (minute 4) must already see long.
    ok = first_long == T0 + 4 * MIN
    info(
        "flag_lag_changed", f"flag changed at {(changed - T0) / MIN if (changed := s_flag_changed(log)) else None} min"
    )
    result("flag_lag", ok, f"fill at {(fill_ts - T0) / MIN:.3f} min; minutes generated with side: {gen}")


def check_throughput() -> None:
    n = 43_200
    candles = {}
    p = 60_000
    for i in range(n):
        o = p
        p += (i * 7919) % 41 - 20
        candles[T0 + i * MIN] = (o, max(o, p) + 5, min(o, p) - 5, p)
    began = time.monotonic()
    s = run(candles, bar_spec="1-HOUR-LAST")
    took = time.monotonic() - began
    events = n * 8
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    info(
        "throughput",
        f"one 30 day month: {events} events in {took:.1f} s = {events / took:,.0f}/s, {len(s.bars)} bars, "
        f"peak RSS {rss:.0f} MB; "
        f"55 months ≈ {55 * took / 60:.1f} min",
    )


# ---------------------------------------------------------------- G-3b deltas (2026-10-17 revision)


def quiet_quote(m: int, price: int) -> list:
    """A quiet minute (AC-9): one quote at m + 59.999 s around its price, no trade."""
    ts = m + 60 * SEC - MS
    b, a = bid_ask(price)
    return [QuoteTick(INSTRUMENT_ID, Price(b, 0), Price(a, 0), QUOTE_SIZE, QUOTE_SIZE, ts, ts)]


def stream_b(candles: dict, quiet: set[int], flag: SideFlag) -> Iterator[list]:
    for m in sorted(candles):
        yield quiet_quote(m, candles[m][3]) if m in quiet else path_events(m, candles[m], flag.side)


class StaleConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_spec: str
    stale_quote_s: int
    stale_check_s: int


class StaleProbe(Strategy):
    """The PlaceholderTimed stale rule with a configurable check interval, logging each bar."""

    def __init__(self, config: StaleConfig) -> None:
        super().__init__(config)
        self.blocked = False
        self.started = 0
        self.bars: list[Bar] = []
        self.bar_blocked: list[tuple[int, bool, str]] = []
        self.trades = 0
        self.checks = 0

    def on_start(self) -> None:
        self.started = self.clock.timestamp_ns()
        self.subscribe_quote_ticks(self.config.instrument_id)
        self.subscribe_trade_ticks(self.config.instrument_id)
        self.subscribe_bars(BarType.from_str(f"{self.config.instrument_id}-{self.config.bar_spec}-INTERNAL"))
        self.clock.set_timer_ns("stale", self.config.stale_check_s * SEC, 0, 0, self.on_check)

    def on_check(self, event) -> None:
        self.checks += 1
        q = self.cache.quote_tick(self.config.instrument_id)
        last = self.started if q is None else q.ts_event
        self.blocked = self.clock.timestamp_ns() - last > self.config.stale_quote_s * SEC

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.trades += 1

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        self.bar_blocked.append(((bar.ts_event - T0) // MIN, self.blocked, str(bar.volume)))


class BarCounter(Actor):
    """A second subscriber to the strategy's internal bar type (LabRecorder's zero_volume_bars)."""

    def __init__(self, bar_type: BarType) -> None:
        super().__init__(FlagConfig())
        self.bar_type = bar_type
        self.bars: list[Bar] = []

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)


def run_b(candles, quiet=frozenset(), *, stale_check_s=60, stale_quote_s=300, counter=False):
    engine = make_engine()
    flag = SideFlag()
    s = StaleProbe(
        StaleConfig(
            instrument_id=INSTRUMENT_ID,
            bar_spec="1-HOUR-LAST",
            stale_quote_s=stale_quote_s,
            stale_check_s=stale_check_s,
        )
    )
    engine.add_actor(PositionSideFlag(flag))
    c = BarCounter(BarType.from_str(f"{INSTRUMENT_ID}-1-HOUR-LAST-INTERNAL")) if counter else None
    if c is not None:
        engine.add_actor(c)
    engine.add_strategy(s)
    engine.add_data_iterator("candles", stream_b(candles, set(quiet), flag))
    began = time.monotonic()
    engine.run(start=min(candles), end=max(candles) + 2 * MIN)
    took = time.monotonic() - began
    aggregators = getattr(engine.kernel.data_engine, "_bar_aggregators", None)
    engine.dispose()
    return s, c, took, aggregators


def check_stale_hour_close() -> None:
    # Three hours of normal minutes, then a 7 minute gap right before the 04:00 close, then on.
    candles = flat_minutes(T0, 300)
    for k in range(4 * 60 - 7, 4 * 60):
        candles.pop(T0 + k * MIN)
    s, _, _, _ = run_b(candles)
    normal = [b for m, b, _ in s.bar_blocked if m in (60, 120, 180)]
    gap = [b for m, b, _ in s.bar_blocked if m == 240]
    s1, _, _, _ = run_b(candles, stale_check_s=1, stale_quote_s=10)
    paper_rule = [b for m, b, _ in s1.bar_blocked if m in (60, 120, 180)]
    ok = normal == [False, False, False] and gap == [True]
    result(
        "stale_300s_60s_check",
        ok,
        f"hour closes 01-03 blocked {normal}; 04:00 after a 7 min gap blocked {gap}; "
        f"for contrast the paper rule (10 s, 1 s check) blocks hour closes 01-03: {paper_rule}",
    )


def check_quiet_hour() -> None:
    # Hour 01:00-02:00 entirely quiet minutes at 60_000; hours 00 and 02 traded.
    candles = flat_minutes(T0, 180)
    quiet = {T0 + k * MIN for k in range(60, 120)}
    for m in quiet:
        candles[m] = (60_000, 60_000, 60_000, 60_000)
    s, c, _, aggregators = run_b(candles, quiet, counter=True)
    vols = {m: v for m, _, v in s.bar_blocked}
    blocked_quiet = [b for m, b, _ in s.bar_blocked if m == 120]
    expected_trades = 120 * 4
    same = c is not None and len(c.bars) == len(s.bars) and all(a is b for a, b in zip(c.bars, s.bars, strict=True))
    equal = c is not None and [str(b) for b in c.bars] == [str(b) for b in s.bars]
    ok = vols.get(120) == "0.0000" and s.trades == expected_trades and not blocked_quiet[0] and equal
    result(
        "quiet_hour_zero_volume_bar",
        ok,
        f"bars (min, blocked, volume): {s.bar_blocked}; trades {s.trades} (expected {expected_trades}, "
        f"no trade from 60 quiet minutes); quiet hour bar blocked {blocked_quiet}",
    )
    result(
        "second_subscriber_same_bars",
        equal,
        f"strategy bars {len(s.bars)}, actor bars {len(c.bars) if c else None}, same objects {same}, "
        f"equal {equal}; data engine aggregators: "
        f"{None if aggregators is None else list(aggregators)}",
    )


def check_stale_throughput() -> None:
    n = 43_200
    candles = {}
    p = 60_000
    for i in range(n):
        o = p
        p += (i * 7919) % 41 - 20
        candles[T0 + i * MIN] = (o, max(o, p) + 5, min(o, p) - 5, p)
    _, _, took60, _ = run_b(candles, stale_check_s=60)
    s1, _, took1, _ = run_b(candles, stale_check_s=1)
    events = n * 8
    info(
        "stale_throughput",
        f"one 30 day month: 60 s check {took60:.1f} s ({events / took60:,.0f} data events/s), "
        f"1 s check {took1:.1f} s ({events / took1:,.0f}/s, {s1.checks} timer events); "
        f"55 months ≈ {55 * took60 / 60:.1f} min with the 60 s check",
    )


def engine_b() -> None:
    for check in (check_stale_hour_close, check_quiet_hour, check_stale_throughput):
        try:
            check()
        except Exception as exc:
            result(check.__name__, False, f"raised {type(exc).__name__}: {exc}")
    print("\n".join(f"{k}: {v}" for k, v in RESULTS.items()))


def engine() -> None:
    for check in (
        check_zero_trade,
        check_timer_order,
        check_market_on_bar_close,
        check_stop_fill,
        check_limit_trade_through,
        check_internal_bars,
        check_flag_lag,
        check_throughput,
    ):
        try:
            check()
        except Exception as exc:
            result(check.__name__, False, f"raised {type(exc).__name__}: {exc}")
    print("\n".join(f"{k}: {v}" for k, v in RESULTS.items()))
