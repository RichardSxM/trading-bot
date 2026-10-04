"""Kraken Futures client configs.

Kraken shut down its futures demo on 14 July 2026, so market data always comes from
Kraken's real public feeds (no API keys needed). Demo and live differ only in where
orders go: the sandbox in demo (feature 6), Kraken in live (spec 0003, allowed from feature 19).
"""

from nautilus_trader.adapters.kraken import (
    KrakenDataClientConfig,
    KrakenEnvironment,
    KrakenExecClientConfig,
    KrakenProductType,
)
from nautilus_trader.config import InstrumentProviderConfig

# The BTC perpetual the paper loop trades (spec 0006).
BTC_PERP = "PF_XBTUSD.KRAKEN"


def futures_data_client() -> KrakenDataClientConfig:
    """Public market data only; it never carries API keys."""
    return KrakenDataClientConfig(
        environment=KrakenEnvironment.LIVE,
        product_types=(KrakenProductType.FUTURES,),
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )


def futures_exec_client(
    api_key: str,
    api_secret: str,
    base_url_http: str | None = None,
    base_url_ws: str | None = None,
) -> KrakenExecClientConfig:
    """The live Kraken Futures execution client, keys set explicitly from the secret files (spec 0003).

    Explicit keys mean the adapter never falls back to its own environment lookup. The URL
    overrides exist only for the canary leak test, which points them at a closed local port.
    """
    return KrakenExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        environment=KrakenEnvironment.LIVE,
        product_types=(KrakenProductType.FUTURES,),
        instrument_provider=InstrumentProviderConfig(load_all=True),
        base_url_http_futures=base_url_http,
        base_url_ws_futures=base_url_ws,
    )
