"""The sandbox execution client: demo orders fill in a simulated exchange inside the trader (spec 0006).

Kraken closed its futures demo, so demo trades against Kraken's real public quotes while
NautilusTrader's sandbox does the matching. It fills on quotes only (``bar_execution`` and
``trade_execution`` off). The sandbox config has no fee field, and Kraken publishes its
instruments with zero fees, so this client sets the configured maker and taker fees on every
instrument it receives (the AC-1 spike, recorded in the spec's ``verify.md``).
"""

import asyncio
from decimal import Decimal

from nautilus_trader.adapters.kraken import KRAKEN
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.data import Data
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.portfolio.base import PortfolioFacade

from trading_core.settings import PaperLoopSettings

# The sandbox simulates USD margin; Kraken's PF_ perpetuals settle in USD.
SANDBOX_CURRENCY = "USD"


class FeeSandboxConfig(SandboxExecutionClientConfig, frozen=True, kw_only=True):
    """The sandbox config plus the fees, as fractions of notional, set on every instrument it trades."""

    maker_fee: Decimal
    taker_fee: Decimal


def with_fees(instrument: Instrument, maker_fee: Decimal, taker_fee: Decimal) -> Instrument:
    """A copy of ``instrument`` with the given maker and taker fees, everything else unchanged."""
    cls = type(instrument)
    values = cls.to_dict(instrument)
    values["maker_fee"] = str(maker_fee)
    values["taker_fee"] = str(taker_fee)
    patched: Instrument = cls.from_dict(values)
    return patched


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

    def connect(self) -> None:
        """Connect, then reset the fees on every instrument the sandbox loaded from the cache."""
        super().connect()
        for instrument in self.exchange.cache.instruments(venue=self.venue):
            self.exchange.update_instrument(with_fees(instrument, self._maker_fee, self._taker_fee))

    def on_data(self, data: Data) -> None:
        """Feed market data to the simulated exchange; an instrument update keeps the configured fees."""
        if isinstance(data, Instrument):
            data = with_fees(data, self._maker_fee, self._taker_fee)
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
    )
