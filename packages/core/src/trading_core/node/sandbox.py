"""The sandbox execution client: demo orders fill in a simulated exchange inside the trader (spec 0006).

Kraken closed its futures demo, so demo trades against Kraken's real public quotes while
NautilusTrader's sandbox does the matching. It fills on quotes only (``bar_execution`` and
``trade_execution`` off). The sandbox config has no fee field, and Kraken publishes its
instruments with zero fees, so this client sets the configured maker and taker fees on every
instrument it receives (the AC-1 spike, recorded in the spec's ``verify.md``).

For the AC-7 drill only, ``SANDBOX_REJECT_STOP_ORDERS`` makes it reject every stop order, so you
can watch the strategy flatten and block entries when its stop does not rest.

For spec 0010's AC-1 drill only, ``SANDBOX_DELAY_ENTRY_FILLS_S`` holds every order that is not
reduce only for that many seconds before it reaches the simulated exchange, so a stop can cancel an
entry in flight. A held order reads submitted at once; a cancel during the hold cancels it without a
venue order id, like a venue cancelling an order it has not worked yet.

The simulated exchange sees every quote with a deep top of book (``deep_quote``). A market order
larger than the quoted size fills the quoted size, and Nautilus fills the rest one tick worse only
if the cached order already reads open. In a live node the execution engine queues events, so the
order still reads ``SUBMITTED`` and the rest is dropped without any event (seen on 2026-10-05: 0.0002
of a 0.0011 entry filled, the rest left a ghost open order). The orders here are far smaller than
Kraken's real top of book, so filling them in full at the best price is the faithful simulation.
"""

import asyncio
import logging
from decimal import Decimal

from nautilus_trader.adapters.kraken import KRAKEN
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.data import Data
from nautilus_trader.execution.messages import CancelOrder, SubmitOrder
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.portfolio.base import PortfolioFacade

from trading_core.settings import PaperLoopSettings

# The sandbox simulates USD margin; Kraken's PF_ perpetuals settle in USD.
SANDBOX_CURRENCY = "USD"
DRILL_REJECT_REASON = "SANDBOX_REJECT_STOP_ORDERS drill"
KILL_SWITCH_DRILL_REASON = "SANDBOX_REJECT_KILL_SWITCH_ORDERS drill"
DELAY_RELEASE_FAILED_REASON = "SANDBOX_DELAY_ENTRY_FILLS_S drill release failed"
KILL_SWITCH_TAG = "origin:kill_switch"
# The least size, in base units, the simulated exchange sees on each side of every quote.
DEEP_BOOK_SIZE = 1000

log = logging.getLogger(__name__)


class FeeSandboxConfig(SandboxExecutionClientConfig, frozen=True, kw_only=True):
    """The sandbox config plus the fees, as fractions of notional, set on every instrument it trades.

    ``reject_every_stop`` is the AC-7 drill switch. It differs from Nautilus' own
    ``reject_stop_orders``, which rejects only a stop whose trigger is already in the market.
    ``reject_kill_switch`` rejects every order tagged ``origin:kill_switch`` (spec 0010's drill).
    ``delay_entry_fills_s`` above 0 holds every order that is not reduce only that many seconds
    before it reaches the exchange (spec 0010's AC-1 drill).
    """

    maker_fee: Decimal
    taker_fee: Decimal
    reject_every_stop: bool = False
    reject_kill_switch: bool = False
    delay_entry_fills_s: Decimal = Decimal(0)


def with_fees(instrument: Instrument, maker_fee: Decimal, taker_fee: Decimal) -> Instrument:
    """A copy of ``instrument`` with the given maker and taker fees, everything else unchanged."""
    cls = type(instrument)
    values = cls.to_dict(instrument)
    values["maker_fee"] = str(maker_fee)
    values["taker_fee"] = str(taker_fee)
    patched: Instrument = cls.from_dict(values)
    return patched


def deep_quote(quote: QuoteTick) -> QuoteTick:
    """``quote`` with each side's size raised to at least ``DEEP_BOOK_SIZE``, prices and times unchanged."""
    return QuoteTick(
        instrument_id=quote.instrument_id,
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        bid_size=max(quote.bid_size, Quantity(DEEP_BOOK_SIZE, quote.bid_size.precision)),
        ask_size=max(quote.ask_size, Quantity(DEEP_BOOK_SIZE, quote.ask_size.precision)),
        ts_event=quote.ts_event,
        ts_init=quote.ts_init,
    )


class FeeSandboxExecutionClient(SandboxExecutionClient):
    """The Nautilus sandbox client, charging the configured fees instead of the instrument's own."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        portfolio: PortfolioFacade,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: FeeSandboxConfig,
    ) -> None:
        super().__init__(loop=loop, portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock, config=config)
        self._maker_fee = config.maker_fee
        self._taker_fee = config.taker_fee
        self._reject_every_stop = config.reject_every_stop
        self._reject_kill_switch = config.reject_kill_switch
        self._delay_entry_fills_s = config.delay_entry_fills_s
        # Held entries by id, with the timer that releases each; one map, so nothing emits twice.
        self._held: dict[ClientOrderId, tuple[SubmitOrder, asyncio.TimerHandle]] = {}
        self._drill_canceled: set[ClientOrderId] = set()

    def connect(self) -> None:
        """Connect, then reset the fees on every instrument the sandbox loaded from the cache."""
        super().connect()
        for instrument in self.exchange.cache.instruments(venue=self.venue):
            self.exchange.update_instrument(with_fees(instrument, self._maker_fee, self._taker_fee))

    def disconnect(self) -> None:
        """Cancel every held entry, so a shutdown leaves no submitted order and no timer behind, then disconnect."""
        for client_order_id in list(self._held):
            self._cancel_held(client_order_id)
        super().disconnect()

    def submit_order(self, command: SubmitOrder) -> None:
        """Submit to the simulated exchange; in a drill a stop or kill switch order is rejected, or an entry held."""
        order = command.order
        ts = self.test_clock.timestamp_ns()  # the sandbox's own event clock, as on every other event
        if self._reject_every_stop and order.has_trigger_price:
            reason = DRILL_REJECT_REASON
        elif self._reject_kill_switch and KILL_SWITCH_TAG in (order.tags or []):
            reason = KILL_SWITCH_DRILL_REASON
        elif self._delay_entry_fills_s > 0 and not order.is_reduce_only:
            self.generate_order_submitted(command.strategy_id, command.instrument_id, order.client_order_id, ts)
            # A duration, not money: the event loop takes float seconds.
            timer = self._loop.call_later(float(self._delay_entry_fills_s), self._release, order.client_order_id)
            self._held[order.client_order_id] = (command, timer)
            return
        else:
            super().submit_order(command)  # type: ignore[no-untyped-call]  # Nautilus' sandbox client is unannotated
            return
        self.generate_order_submitted(command.strategy_id, command.instrument_id, order.client_order_id, ts)
        self.generate_order_rejected(command.strategy_id, command.instrument_id, order.client_order_id, reason, ts)

    def cancel_order(self, command: CancelOrder) -> None:
        """Cancel a held entry here; a second cancel of one is dropped; any other order goes to the exchange."""
        if command.client_order_id in self._drill_canceled:
            return
        if command.client_order_id in self._held:
            self._cancel_held(command.client_order_id)
            return
        super().cancel_order(command)  # type: ignore[no-untyped-call]  # Nautilus' sandbox client is unannotated

    def _release(self, client_order_id: ClientOrderId) -> None:
        """Send a held entry to the exchange, straight past the inner client, which would emit submitted again."""
        held = self._held.pop(client_order_id, None)
        if held is None:
            return
        command, _ = held
        try:
            self.exchange.send(command)
        except Exception as exc:
            # Reject it, so the strategy goes back to flat instead of keeping a ghost submitted order.
            log.error(
                "held entry not released to the sandbox exchange",
                extra={
                    "fields": {
                        "code": "sandbox_delay_release_failed",
                        "client_order_id": str(client_order_id),
                        "error": type(exc).__name__,
                    }
                },
            )
            self.generate_order_rejected(
                command.strategy_id,
                command.instrument_id,
                client_order_id,
                DELAY_RELEASE_FAILED_REASON,
                self.test_clock.timestamp_ns(),
            )

    def _cancel_held(self, client_order_id: ClientOrderId) -> None:
        """Drop a held entry's timer and command and emit its cancel, with no venue order id."""
        command, timer = self._held.pop(client_order_id)
        timer.cancel()
        self._drill_canceled.add(client_order_id)
        self.generate_order_canceled(
            command.strategy_id,
            command.instrument_id,
            client_order_id,
            None,  # never reached the matching engine, so no venue order id
            self.test_clock.timestamp_ns(),
        )

    def on_data(self, data: Data) -> None:
        """Feed market data to the simulated exchange: quotes as ``deep_quote``, instruments with the fees."""
        if isinstance(data, Instrument):
            data = with_fees(data, self._maker_fee, self._taker_fee)
        elif isinstance(data, QuoteTick):
            data = deep_quote(data)
        super().on_data(data)


class FeeSandboxExecClientFactory(LiveExecClientFactory):
    """Builds ``FeeSandboxExecutionClient`` for the node (registered under ``env_guard.SANDBOX``)."""

    @staticmethod
    def create(  # type: ignore[override]  # the base types config as LiveExecClientConfig
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: FeeSandboxConfig,
        portfolio: PortfolioFacade,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> FeeSandboxExecutionClient:
        """Create the client; ``name`` is unused, the client id is the venue."""
        return FeeSandboxExecutionClient(
            loop=loop, portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock, config=config
        )


# NautilusTrader 1.231's node builder passes ``portfolio`` only to a factory whose class name is
# exactly ``SandboxLiveExecClientFactory`` (``TradingNodeBuilder.build_exec_clients``), and the
# sandbox client cannot be built without it. Recheck this when NautilusTrader is upgraded.
FeeSandboxExecClientFactory.__name__ = "SandboxLiveExecClientFactory"


def sandbox_exec_client(settings: PaperLoopSettings) -> FeeSandboxConfig:
    """The demo trader's sandbox: Kraken venue, USD margin account, netting, leverage at the risk cap."""
    return FeeSandboxConfig(
        venue=KRAKEN,
        starting_balances=[f"{settings.sandbox_starting_balance_usd} {SANDBOX_CURRENCY}"],
        base_currency=SANDBOX_CURRENCY,
        oms_type="NETTING",
        account_type="MARGIN",
        default_leverage=settings.risk_max_leverage,
        bar_execution=False,
        trade_execution=False,
        maker_fee=settings.sandbox_maker_fee,
        taker_fee=settings.sandbox_taker_fee,
        reject_every_stop=settings.sandbox_reject_stop_orders,
        reject_kill_switch=settings.sandbox_reject_kill_switch_orders,
        delay_entry_fills_s=settings.sandbox_delay_entry_fills_s,
    )
