"""covers: spec 0003 AC-7 (the one Kraken execution client factory, keys passed explicitly).

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image.
"""

import os

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.kraken import KrakenEnvironment, KrakenProductType

from trading_core.node.kraken import futures_data_client, futures_exec_client


def test_exec_client_carries_the_given_keys_for_live_futures_only():
    key, secret = os.urandom(16).hex(), os.urandom(32).hex()

    config = futures_exec_client(key, secret)

    assert (config.api_key, config.api_secret) == (key, secret)
    assert config.environment == KrakenEnvironment.LIVE
    assert config.product_types == (KrakenProductType.FUTURES,)


def test_exec_client_keeps_kraken_urls_unless_told_otherwise():
    config = futures_exec_client("k" * 16, "s" * 16)

    assert (config.base_url_http_futures, config.base_url_ws_futures) == (None, None)


def test_exec_client_url_overrides_reach_only_the_futures_endpoints():
    config = futures_exec_client("k" * 16, "s" * 16, "http://127.0.0.1:9", "ws://127.0.0.1:9")

    assert (config.base_url_http_futures, config.base_url_ws_futures) == ("http://127.0.0.1:9", "ws://127.0.0.1:9")
    assert (config.base_url_http_spot, config.base_url_ws_spot) == (None, None)


def test_data_client_never_carries_keys():
    config = futures_data_client()

    assert (config.api_key, config.api_secret) == (None, None)
