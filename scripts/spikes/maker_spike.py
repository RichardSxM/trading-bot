"""Spec 0026 task 2 spike: post only rejections and two resting reduce only orders, on both venues.

Two venues, each fed scripted quotes for PF_XBTUSD (the pinned lab instrument):
  lab:     the candle lab's own engine (``lab.candle_units.make_candle_engine``)
  sandbox: a backtest venue built from the demo trader's ``sandbox_exec_client`` settings (the same
           ``SimulatedExchange`` arguments the Nautilus sandbox client passes), quotes through ``deep_quote``

Scenarios, each printed as one JSON line per venue:
  post_only_cross:   a post only buy limit at the ask and a post only sell limit at the bid (both would take)
  limit_then_stop:   a long under a reduce only stop market and a reduce only post only exit limit; the price
                     reaches the limit first, then falls through the stop
  stop_then_limit:   the same two orders; the price falls through the stop first, then reaches the limit
Run in the lab image:
  docker run --rm -v "$PWD/scripts/spikes:/spike:ro" --entrypoint python kfs/lab:<tag> -I /spike/maker_spike.py
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.events import OrderEvent, OrderRejected
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from lab.candle_units import make_candle_engine
from lab.instruments import with_fees
from trading_core.node.sandbox import deep_quote, sandbox_exec_client

ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
NS = 1_000_000_000
QTY = "0.0100"


@dataclass
class Step:
    """At quote number ``at``, do ``action``."""

    at: int
    action: str


class Spike(Strategy):
    """Runs one scenario's actions at fixed quote counts and keeps every order event."""

    def __init__(self, steps: list[Step]) -> None:
        super().__init__(StrategyConfig(strategy_id="SPIKE-001"))
        self.steps = steps
        self.n = 0
        self.events: list[dict[str, object]] = []

    def on_start(self) -> None:
        self.subscribe_quote_ticks(ID)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.n += 1
        for step in self.steps:
            if step.at == self.n:
                getattr(self, step.action)(tick)

    def cross(self, tick: QuoteTick) -> None:
        buy = self.order_factory.limit(ID, OrderSide.BUY, Quantity.from_str(QTY), tick.ask_price, post_only=True)
        sell = self.order_factory.limit(ID, OrderSide.SELL, Quantity.from_str(QTY), tick.bid_price, post_only=True)
        self.submit_order(buy)
        self.submit_order(sell)

    def open_long(self, tick: QuoteTick) -> None:
        self.submit_order(self.order_factory.market(ID, OrderSide.BUY, Quantity.from_str(QTY)))

    def protect(self, tick: QuoteTick) -> None:
        stop = self.order_factory.stop_market(
            ID,
            OrderSide.SELL,
            Quantity.from_str(QTY),
            trigger_price=Price(tick.bid_price.as_double() - 100, 0),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
        )
        exit_limit = self.order_factory.limit(
            ID,
            OrderSide.SELL,
            Quantity.from_str(QTY),
            Price(tick.ask_price.as_double() + 50, 0),
            post_only=True,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(stop)
        self.submit_order(exit_limit)

    def on_order_event(self, event: OrderEvent) -> None:
        row: dict[str, object] = {"n": self.n, "event": type(event).__name__, "id": str(event.client_order_id)}
        if isinstance(event, OrderRejected):
            row |= {"due_post_only": event.due_post_only, "reason": str(event.reason)}
        self.events.append(row)


def quotes(mids: list[int], deep: bool) -> list[QuoteTick]:
    out = []
    for i, mid in enumerate(mids):
        ts = (1_800_000_000 + i) * NS
        q = QuoteTick(
            ID, Price(mid, 0), Price(mid + 1, 0), Quantity.from_str("1.0000"), Quantity.from_str("1.0000"), ts, ts
        )
        out.append(deep_quote(q) if deep else q)
    return out


def lab_engine() -> BacktestEngine:
    plan = SimpleNamespace(
        instrument_ids=(str(ID),),
        starting_balance_usd=Decimal(10000),
        leverage=Decimal(5),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
    )
    return make_candle_engine(plan)  # type: ignore[arg-type]


def sandbox_engine() -> BacktestEngine:
    settings = SimpleNamespace(
        sandbox_starting_balance_usd=10000,
        risk_max_leverage=Decimal(5),
        sandbox_maker_fee=Decimal("0.0002"),
        sandbox_taker_fee=Decimal("0.0005"),
        sandbox_reject_stop_orders=False,
        sandbox_reject_kill_switch_orders=False,
        sandbox_delay_entry_fills_s=Decimal(0),
    )
    c = sandbox_exec_client(settings)  # type: ignore[arg-type]
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=Venue(c.venue),
        oms_type=OmsType[c.oms_type],
        account_type=AccountType[c.account_type],
        starting_balances=[Money.from_str(b) for b in c.starting_balances],
        base_currency=USD,
        default_leverage=c.default_leverage,
        fill_model=FillModel(),
        fee_model=MakerTakerFeeModel(),
        book_type=BookType[c.book_type],
        frozen_account=c.frozen_account,
        bar_execution=c.bar_execution,
        trade_execution=c.trade_execution,
        reject_stop_orders=c.reject_stop_orders,
        support_gtd_orders=c.support_gtd_orders,
        support_contingent_orders=c.support_contingent_orders,
        use_position_ids=c.use_position_ids,
        use_random_ids=c.use_random_ids,
        use_reduce_only=c.use_reduce_only,
    )
    engine.add_instrument(with_fees(str(ID), c.maker_fee, c.taker_fee))
    return engine


SCENARIOS = {
    "post_only_cross": ([85000] * 5, [Step(2, "cross")]),
    # long at 85001 (the ask); stop at 84900, exit limit at 85051
    "limit_then_stop": (
        [85000] * 4 + [85020, 85060, 85060, 85000, 84800, 84700, 84700],
        [Step(2, "open_long"), Step(3, "protect")],
    ),
    "stop_then_limit": (
        [85000] * 4 + [84950, 84800, 84700, 84900, 85100, 85200, 85200],
        [Step(2, "open_long"), Step(3, "protect")],
    ),
}


def run(venue: str, scenario: str) -> dict[str, object]:
    mids, steps = SCENARIOS[scenario]
    engine = lab_engine() if venue == "lab" else sandbox_engine()
    engine.add_data(quotes(mids, deep=venue == "sandbox"))
    spike = Spike(steps)
    engine.add_strategy(spike)
    engine.run()
    orders = {
        str(o.client_order_id): {"type": o.type_string(), "status": o.status_string(), "filled": str(o.filled_qty)}
        for o in engine.cache.orders()
    }
    positions = [{"side": str(p.side), "qty": str(p.quantity), "closed": p.is_closed} for p in engine.cache.positions()]
    open_positions = [str(p.quantity) for p in engine.cache.positions_open()]
    return {
        "venue": venue,
        "scenario": scenario,
        "orders": orders,
        "positions": positions,
        "open_positions": open_positions,
        "events": spike.events,
    }


if __name__ == "__main__":
    for venue in ("lab", "sandbox"):
        for scenario in SCENARIOS:
            print(json.dumps(run(venue, scenario)))
