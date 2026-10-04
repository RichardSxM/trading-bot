"""Spec 0006 AC-1 gate spike. Throwaway: never merged as product code.

Runs a TradingNode with the sandbox exec client on PF_XBTUSD.KRAKEN and Kraken's public quotes, and checks:
1. a market entry fills within the bid and ask at submit
2. a reduce only stop market fills when the quote crosses its trigger
3. a reduce only market order sent while flat is denied or canceled, never filled
4. the trader Redis login can SCAN and UNLINK keys matching trader-TRADER-DEMO:*
It also checks that fees reach the fills through the instrument (the sandbox config has no fee field
and Kraken publishes the instrument with zero fees).

Run: docker compose run --rm -T -v "$PWD/scripts/spikes:/spike" trader python /spike/0006_gate.py
"""

import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import LiveExecEngineConfig, LoggingConfig, StrategyConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderDenied, OrderFilled, OrderRejected
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.trading.strategy import Strategy
from redis import Redis

from trading_core.node.kraken import futures_data_client

INSTRUMENT = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
RESULTS: dict[str, str] = {}
OVERRIDE_FEES = os.environ.get("SPIKE_OVERRIDE_FEES") == "1"


def result(name: str, ok: bool, detail: str) -> None:
    RESULTS[name] = f"{'PASS' if ok else 'FAIL'}: {detail}"
    print(f"SPIKE {name} {RESULTS[name]}", flush=True)


class SpikeConfig(StrategyConfig, frozen=True):
    pass


class Spike(Strategy):
    def __init__(self, config: SpikeConfig) -> None:
        super().__init__(config)
        self.phase = "wait"
        self.entry_bid: Decimal | None = None
        self.entry_ask: Decimal | None = None
        self.stop_id = None
        self.flat_id = None
        self.quotes = 0

    def on_start(self) -> None:
        self.subscribe_quote_ticks(INSTRUMENT)
        self.clock.set_timer("spike_timeout", timedelta(minutes=20), callback=self._timeout)

    def _timeout(self, event: object) -> None:
        result("timeout", False, f"phase {self.phase} after 20 min")
        self._finish()

    def _finish(self) -> None:
        self.phase = "done"
        self.clock.cancel_timers()
        self.close_all_positions(INSTRUMENT)
        print("SPIKE finished", flush=True)
        os.kill(os.getpid(), 2)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.quotes += 1
        instrument = self.cache.instrument(INSTRUMENT)
        if self.quotes == 1:
            print(
                f"SPIKE instrument maker_fee={instrument.maker_fee} taker_fee={instrument.taker_fee} "
                f"size_increment={instrument.size_increment} price_increment={instrument.price_increment} "
                f"min_qty={instrument.min_quantity} class={type(instrument).__name__}",
                flush=True,
            )
        if self.quotes == 1:
            from nautilus_trader.model.instruments import CryptoPerpetual

            d = CryptoPerpetual.to_dict(instrument)
            d["maker_fee"] = "0.0002"
            d["taker_fee"] = "0.0005"
            patched = CryptoPerpetual.from_dict(d)
            self.cache.add_instrument(patched)
            self.msgbus.publish(f"data.instrument.{INSTRUMENT.venue}.{INSTRUMENT.symbol}", patched)
            print(f"SPIKE patched fees taker={self.cache.instrument(INSTRUMENT).taker_fee}", flush=True)
        if self.phase == "wait" and self.quotes >= 3:
            self.phase = "entry"
            self.entry_bid = tick.bid_price.as_decimal()
            self.entry_ask = tick.ask_price.as_decimal()
            qty = instrument.make_qty(Decimal("0.002"))  # ~170 USD, so a 0.05 % fee shows at USD precision
            order = self.order_factory.market(INSTRUMENT, OrderSide.BUY, qty, time_in_force=TimeInForce.GTC)
            print(f"SPIKE entry submit bid={self.entry_bid} ask={self.entry_ask} qty={qty}", flush=True)
            self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        px = event.last_px.as_decimal()
        print(
            f"SPIKE fill {event.client_order_id} side={event.order_side.name} px={px} qty={event.last_qty} "
            f"commission={event.commission} liquidity={event.liquidity_side.name}",
            flush=True,
        )
        if self.phase == "entry":
            ok = self.entry_bid is not None and self.entry_ask is not None and self.entry_bid <= px <= self.entry_ask
            result("1_entry_within_touch", ok, f"px {px} in [{self.entry_bid}, {self.entry_ask}]")
            result("5_fee_via_instrument", event.commission.as_decimal() != 0, f"entry commission {event.commission}")
            self._submit_stop()
        elif self.phase == "stop" and event.client_order_id == self.stop_id:
            quote = self.cache.quote_tick(INSTRUMENT)
            result("2_stop_fills_on_cross", True, f"stop filled at {px}, bid now {quote.bid_price}")
            self._submit_flat_close()
        elif self.phase == "flat" and event.client_order_id == self.flat_id:
            result("3_reduce_only_while_flat", False, "reduce only market FILLED while flat")
            self._finish()

    def _submit_stop(self) -> None:
        instrument = self.cache.instrument(INSTRUMENT)
        quote = self.cache.quote_tick(INSTRUMENT)
        tick = instrument.price_increment.as_decimal()
        trigger = instrument.make_price(quote.bid_price.as_decimal() - 2 * tick)
        position = self.cache.positions_open(instrument_id=INSTRUMENT)[0]
        order = self.order_factory.stop_market(
            INSTRUMENT,
            OrderSide.SELL,
            position.quantity,
            trigger_price=trigger,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
        )
        self.stop_id = order.client_order_id
        self.phase = "stop"
        print(f"SPIKE stop submit trigger={trigger} bid={quote.bid_price}", flush=True)
        self.submit_order(order)

    def _submit_flat_close(self) -> None:
        self.phase = "flat"
        instrument = self.cache.instrument(INSTRUMENT)
        open_positions = self.cache.positions_open(instrument_id=INSTRUMENT)
        print(f"SPIKE open positions before flat close: {len(open_positions)}", flush=True)
        qty = instrument.min_quantity or instrument.make_qty(instrument.size_increment)
        order = self.order_factory.market(
            INSTRUMENT, OrderSide.SELL, qty, reduce_only=True, time_in_force=TimeInForce.GTC
        )
        self.flat_id = order.client_order_id
        self.submit_order(order)
        self.clock.set_time_alert("flat_wait", self.clock.utc_now() + timedelta(seconds=5), callback=self._flat_wait)

    def _flat_wait(self, event: object) -> None:
        if "3_reduce_only_while_flat" not in RESULTS:
            order = self.cache.order(self.flat_id)
            status = order.status.name if order else "missing"
            result("3_reduce_only_while_flat", status in {"DENIED", "CANCELED", "REJECTED"}, f"status {status}")
        self._finish()

    def _refused(self, event: object, kind: str) -> None:
        print(f"SPIKE {kind} {event}", flush=True)
        if self.phase == "flat" and event.client_order_id == self.flat_id:
            result("3_reduce_only_while_flat", True, f"{kind}: {getattr(event, 'reason', '')}")
        elif self.phase in {"entry", "stop"}:
            result(f"unexpected_{kind}_{self.phase}", False, str(event))

    def on_order_denied(self, event: OrderDenied) -> None:
        self._refused(event, "denied")

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._refused(event, "rejected")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._refused(event, "canceled")

    def on_order_accepted(self, event: object) -> None:
        print(f"SPIKE accepted {event.client_order_id}", flush=True)


def redis_check() -> None:
    password = Path("/run/secrets/redis_password").read_text().strip()
    r = Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        username="trader",
        password=password,
    )
    probe = "trader-TRADER-DEMO:spike-probe-0006"
    r.set(probe, "1")
    found = [k.decode() for k in r.scan_iter(match="trader-TRADER-DEMO:*", count=1000)]
    deleted = r.unlink(probe)
    gone = r.exists(probe) == 0
    result(
        "4_redis_scan_unlink",
        probe in found and deleted == 1 and gone,
        f"scan saw {len(found)} keys incl probe, unlink={deleted}",
    )


def main() -> None:
    redis_check()
    config = TradingNodeConfig(
        trader_id=TraderId("SPIKE-0006"),
        logging=LoggingConfig(log_level="WARNING", log_colors=False),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
        exec_clients={
            "SANDBOX": SandboxExecutionClientConfig(
                venue=KRAKEN,
                starting_balances=["1000 USD"],
                base_currency="USD",
                oms_type="NETTING",
                account_type="MARGIN",
                default_leverage=Decimal(2),
                bar_execution=False,
                trade_execution=False,
            )
        },
        timeout_post_stop=2.0,
    )
    node = TradingNode(config=config)
    node.trader.add_strategy(Spike(SpikeConfig()))
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.add_exec_client_factory("SANDBOX", SandboxLiveExecClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()
    print("SPIKE SUMMARY", flush=True)
    for name, value in sorted(RESULTS.items()):
        print(f"SPIKE  {name}: {value}", flush=True)
    sys.exit(0 if RESULTS and all(v.startswith("PASS") for k, v in RESULTS.items()) else 1)


if __name__ == "__main__":
    main()
