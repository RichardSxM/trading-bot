"""Kraken Futures data client config, the same in every environment.

Kraken shut down its futures demo on 14 July 2026, so market data always comes from
Kraken's real public feeds (no API keys needed). Demo and live differ only in where
orders go: the sandbox in demo (feature 6), Kraken in live (feature 4 and the canary).
"""

from nautilus_trader.adapters.kraken import KrakenDataClientConfig, KrakenEnvironment, KrakenProductType
from nautilus_trader.config import InstrumentProviderConfig


def futures_data_client() -> KrakenDataClientConfig:
    """Public market data only; the execution client arrives with features 4 and 6."""
    return KrakenDataClientConfig(
        environment=KrakenEnvironment.LIVE,
        product_types=(KrakenProductType.FUTURES,),
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )
