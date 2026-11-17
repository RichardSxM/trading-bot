"""The lab's book mode (spec 0033 AC-10): one book strategy over every coin of the run in one Nautilus engine.

The candle lab's units run one strategy instance per coin; a book strategy (``trend_vol``) is one instance over
all of them. ``run_book_mode`` builds it from ``lab.strategies.BOOK_REGISTRY`` exactly as the trader does, with a
pause gate (loaded, never paused), entry blocks, the ``record`` callable, the daily close ``seeds`` (the coins'
closes before ``first``, ``seed_days`` of them, from the candles tree) and ``last_start_ns`` None, so its start
rebalance and start cooldown behave as in the trader. ``first`` is the first traded day.

The venue is the candle lab's (spec 0014 AC-8): one margin account, fills only from the candle path's quotes, no
latency; here at no slippage and the run's cost as the taker fee, so a market fill pays exactly the cost. Each
coin's stream also sends its quiet minutes before its first trade (``quiet_lead``), so every coin has a quote, as
on a live venue. The engine starts at ``first`` 00:03 UTC: the start rebalance waits for every eligible coin's
first quote (the 00:03 minute's close at the latest), and the daily run at 00:05 follows, as on a trader started
then. Funding is booked into the account as the venue books it (``FundingModule``), so the strategy sizes
from a balance net of funding, as the trader does and as the pure replay's cash is. ``DailyMarks`` reads, at
every traded day's 00:05 plus 0.9 ms (after that day's rebalance, before the next minute's first point), the
account balance plus every open position's unrealised P&L at its mid, and each coin's signed quantity. A coin
with no candle in the range gets no stream (its instrument is still in the engine; it is never eligible).
Positions are left open at the end.
"""

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.backtest.modules import SimulationModule, SimulationModuleConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money

from lab.actors import LabRecorder, UnitRows
from lab.candle_stream import CandleStream, PositionSideFlag, SideFlag, merged_chunks
from lab.instruments import pinned_instrument, with_fees
from lab.strategies import ConfigValue, RunContext, book_entry
from lab.units import TRADER_ID, UNIT_RUN_ID
from trading_core.catalog.candles import CandleTree
from trading_core.lab.book_parity import BookDay
from trading_core.lab.candles import MINUTE_NS, expected_charge_times
from trading_core.lab.errors import RecordingFailedError
from trading_core.lab.funding import funding_amount
from trading_core.marketdata.hours import SECOND_NS
from trading_core.node import guard
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose, close_day

log = logging.getLogger(__name__)

BTC: Final = "PF_XBTUSD.KRAKEN"
START_OFFSET_NS: Final = 3 * MINUTE_NS
FILL_OFFSET_NS: Final = 5 * MINUTE_NS
# After the 00:05 rebalance's fills (no latency, so at 00:05:00 itself), before the 00:05 minute's open point
# at 00:05:00.001 (``trading_core.lab.candle_path``).
MARK_DELAY_NS: Final = 900_000
DAY_NS: Final = 86_400 * SECOND_NS

# Wide enough that ``check_rebalance`` never clips or refuses a book of gross 5 (the pure replay has no risk
# check): parity compares the rule, not the trader's caps.
BOOK_RISK: Final = RiskLimits(
    max_order_notional=Decimal(10) ** 9,
    max_leverage=Decimal(100),
    max_total_exposure=Decimal(10) ** 9,
    risk_per_trade=Decimal("0.005"),
    maintenance_margin_rate=Decimal("0.01"),
    min_liq_distance_pct=Decimal("0.1"),
    liq_stop_multiple=Decimal(3),
    stop_slippage_bps=Decimal(5),
)


def midnight_ns(day: date) -> int:
    """00:00 UTC of ``day``."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * SECOND_NS


def daily_seed(hourly: Mapping[int, Decimal], first: date, seed_days: int) -> tuple[DailyClose, ...]:
    """A coin's daily closes before ``first``: the hourly close at each 00:00 UTC up to ``first`` 00:00, as the
    day that just ended (``close_day``), the newest ``seed_days`` of them, oldest first."""
    end = midnight_ns(first)
    closes = [DailyClose(close_day(ns), hourly[ns]) for ns in sorted(hourly) if ns % DAY_NS == 0 and ns <= end]
    return tuple(closes[-seed_days:])


@dataclass(frozen=True)
class BookModePlan:
    """One book mode run: the strategy, the coins (sorted), the traded days (both inclusive), the starting
    equity, the cost a side, the strategy settings on top of the registry's, each coin's seed, and each coin's
    funding rates by charge time."""

    strategy: str
    candles_root: Path
    coins: tuple[str, ...]
    first: date
    last: date
    equity: Decimal
    cost: Decimal
    config: Mapping[str, ConfigValue]
    seeds: Mapping[str, tuple[DailyClose, ...]]
    funding: Mapping[str, Mapping[int, Decimal]]
    leverage: Decimal = Decimal(10)
    risk: RiskLimits = BOOK_RISK
    env: Env = Env.DEMO
    stale_quote_s: Decimal = Decimal(300)


@dataclass(frozen=True)
class BookModeResult:
    """The run's traded days (equity with funding, positions), its funding total, what it recorded, and the
    wall time (logs only)."""

    days: tuple[BookDay, ...]
    funding: Decimal
    decisions: int
    fills: int
    funding_missing: int
    wall_seconds: float
    streamed: tuple[str, ...] = field(default=())


class DailyMarksConfig(ActorConfig, frozen=True):
    """No settings: the times and coins are passed to the constructor."""


class DailyMarks(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """At each of ``times``, the account's balance plus the open positions' unrealised P&L at their mids, and
    each coin's signed quantity."""

    def __init__(self, times: Sequence[int], coins: Sequence[InstrumentId], venue: Venue) -> None:
        super().__init__(DailyMarksConfig())
        self._times = tuple(times)
        self._coins = tuple(coins)
        self._venue = venue
        self.marks: list[tuple[int, Decimal, dict[str, Decimal]]] = []

    def on_start(self) -> None:
        """One time alert per mark."""
        for at in self._times:
            self.clock.set_time_alert_ns(name=f"mark-{at}", alert_time_ns=at, callback=self._mark)

    def _mark(self, event: object) -> None:
        if not isinstance(event, TimeEvent):
            raise TypeError(f"unexpected mark alert {type(event).__name__}")
        account = self.portfolio.account(self._venue)
        balance = None if account is None else account.balance_total(USD)
        if balance is None:
            raise RecordingFailedError(f"no account balance at {event.ts_event}")
        equity: Decimal = balance.as_decimal() + guard.unrealized(self.cache)
        positions: dict[str, Decimal] = {}
        for coin in self._coins:
            qty = Decimal(0)
            for position in self.cache.positions_open(instrument_id=coin):
                qty += position.signed_decimal_qty()
            positions[coin.value] = qty
        self.marks.append((event.ts_event, equity, positions))


CENT: Final = Decimal("0.01")


class FundingModuleConfig(SimulationModuleConfig, frozen=True):  # type: ignore[misc,call-arg]  # no stubs
    """No settings: the charge times and rates are passed to the constructor."""


class FundingModule(SimulationModule):  # type: ignore[misc]  # SimulationModule is Cython with no stubs
    """Books perpetual funding into the venue account, as the venue does (spec 0033 AC-10).

    On the first data point at or after each charge time (before the exchange takes it, so every book still
    holds its last quote before the charge time: the hourly close), each coin with an open position and a rate
    pays ``-qty x mid x rate``. The payments add up unrounded and the account moves by the cent rounded change
    of that total, so rounding never builds up. A held coin with no rate at a charge time is counted in
    ``missing`` and pays nothing (as in the pure replay).
    """

    def __init__(
        self, times: Sequence[int], rates: Mapping[str, Mapping[int, Decimal]], coins: Sequence[InstrumentId]
    ) -> None:
        super().__init__(FundingModuleConfig())
        self._times = tuple(sorted(times))
        self._rates = rates
        self._coins = tuple(coins)
        self._next = 0
        self._booked = Decimal(0)
        self.total = Decimal(0)
        self.missing = 0

    def pre_process(self, data: Data) -> None:
        """Charge every charge time up to this data point's time."""
        ts: int = data.ts_init
        while self._next < len(self._times) and self._times[self._next] <= ts:
            self._charge(self._times[self._next])
            self._next += 1

    def _charge(self, at: int) -> None:
        for coin in self._coins:
            qty = Decimal(0)
            for position in self.exchange.cache.positions_open(instrument_id=coin):
                qty += position.signed_decimal_qty()
            if qty == 0:
                continue
            rate = self._rates.get(coin.value, {}).get(at)
            if rate is None:
                self.missing += 1
                continue
            book = self.exchange.get_book(coin)
            bid, ask = book.best_bid_price(), book.best_ask_price()
            if bid is None or ask is None:
                raise RecordingFailedError(f"no quote for {coin} at the funding charge {at}")
            self.total += funding_amount(qty, (bid.as_decimal() + ask.as_decimal()) / 2, rate)
        booked = self.total.quantize(CENT)
        if booked != self._booked:
            self.exchange.adjust_account(Money(booked - self._booked, USD))
            self._booked = booked

    def process(self, ts_now: int) -> None:
        """Nothing: the charges happen in ``pre_process``."""

    def log_diagnostics(self, logger: object) -> None:
        """Nothing to log."""

    def reset(self) -> None:
        """Back to no charge made."""
        self._next = 0
        self._booked = Decimal(0)
        self.total = Decimal(0)
        self.missing = 0


def _engine(plan: BookModePlan, funding: FundingModule) -> BacktestEngine:
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId(TRADER_ID), logging=LoggingConfig(log_level="ERROR"))
    )
    engine.add_venue(
        venue=Venue(InstrumentId.from_str(plan.coins[0]).venue.value),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(plan.equity, USD)],
        base_currency=USD,
        default_leverage=plan.leverage,
        fee_model=MakerTakerFeeModel(),
        book_type=BookType.L1_MBP,
        queue_position=False,
        liquidity_consumption=False,
        trade_execution=False,
        bar_execution=False,
        use_reduce_only=True,
        modules=[funding],
    )
    for coin in plan.coins:
        engine.add_instrument(with_fees(coin, plan.cost, plan.cost))
    return engine


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def run_book_mode(plan: BookModePlan) -> BookModeResult:
    """Run the book over ``plan.first`` to ``plan.last`` (module docstring).

    Raises ``RecordingFailedError`` when an event could not be recorded, or a traded day got no mark.
    """
    started = time.monotonic()
    if plan.last < plan.first:
        raise ValueError(f"last day {plan.last} is before the first {plan.first}")
    book = book_entry(plan.strategy)
    coins = tuple(sorted(plan.coins))
    context_coin = BTC if BTC in coins else coins[0]
    rows = UnitRows()
    gate = PauseGate()
    gate.unpause()  # loaded, not paused: the lab never pauses
    blocks = EntryBlocks()
    context = RunContext(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_id=context_coin,
        seed=0,
        risk=plan.risk,
        order_id_tag=book.order_id_tag,
        taker_fee=plan.cost,
        stale_quote_s=plan.stale_quote_s,
        lab="candle",
        instruments=coins,
    )
    config = book.entry.build_config({**book.config, **plan.config}, context)
    strategy = book.build(config, gate, blocks, rows, dict(plan.seeds), None)
    start_ns = midnight_ns(plan.first) + START_OFFSET_NS
    end_ns = midnight_ns(plan.last) + FILL_OFFSET_NS
    ids = [InstrumentId.from_str(coin) for coin in coins]
    flags = {i: SideFlag() for i in ids}
    streams: list[CandleStream] = []
    streamed: list[str] = []
    for i in ids:
        tree = CandleTree(plan.candles_root, pinned_instrument(i.value))
        stream = CandleStream(tree, start_ns, end_ns, Decimal(0), flags[i], quiet_lead=True)
        if stream.last_point_ns() is not None:
            streams.append(stream)
            streamed.append(i.value)
    if not streams:
        raise RecordingFailedError(f"no coin has a candle from {plan.first} to {plan.last}")
    days = _days(plan.first, plan.last)
    mark_times = [midnight_ns(d) + FILL_OFFSET_NS + MARK_DELAY_NS for d in days]
    venue = ids[0].venue
    marks = DailyMarks(mark_times, ids, venue)
    recorder = LabRecorder(
        env=plan.env,
        run_id=UNIT_RUN_ID,
        instrument_ids=ids,
        rows=rows,
        blocks=blocks,
        risk=plan.risk,
    )
    funding = FundingModule(expected_charge_times(start_ns, end_ns), plan.funding, ids)
    engine = _engine(plan, funding)
    try:
        engine.add_actor(recorder)
        engine.add_actor(PositionSideFlag(flags))
        engine.add_strategy(strategy)
        engine.add_actor(marks)
        engine.add_data_iterator("candles", merged_chunks(streams))
        engine.run(start=start_ns, end=mark_times[-1] + MINUTE_NS)
        if BlockReason.RECORDING_FAILED in blocks.active():
            raise RecordingFailedError("an event could not be recorded")
    finally:
        engine.dispose()
    if len(marks.marks) != len(days):
        raise RecordingFailedError(f"{len(marks.marks)} marks for {len(days)} traded days")
    # The account already holds the funding (``FundingModule``), so the marks are the equity as they are.
    out = [BookDay(day, equity, positions) for day, (_, equity, positions) in zip(days, marks.marks, strict=True)]
    wall = time.monotonic() - started
    log.info(
        "lab_book_mode_done",
        extra={"fields": {"days": len(out), "decisions": len(rows.decisions), "wall_seconds": round(wall, 3)}},
    )
    return BookModeResult(
        days=tuple(out),
        funding=funding.total,
        decisions=len(rows.decisions),
        fills=len(rows.fills),
        funding_missing=funding.missing,
        wall_seconds=wall,
        streamed=tuple(streamed),
    )
