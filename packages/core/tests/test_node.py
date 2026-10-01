"""covers: spec 0001 Environment switch (data always LIVE public feeds) and the node heartbeat.

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image.
"""

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.kraken import KrakenEnvironment, KrakenProductType  # noqa: E402

from trading_core import health  # noqa: E402
from trading_core.node.heartbeat import HeartbeatConfig  # noqa: E402
from trading_core.node.kraken import futures_data_client  # noqa: E402


def test_data_client_reads_kraken_live_public_futures_feeds():
    config = futures_data_client()

    assert config.environment == KrakenEnvironment.LIVE
    assert tuple(config.product_types) == (KrakenProductType.FUTURES,)
    assert config.instrument_provider.load_all is True


def test_data_client_carries_no_api_keys():
    config = futures_data_client()

    assert not getattr(config, "api_key", None)
    assert not getattr(config, "api_secret", None)


def test_heartbeat_writes_where_the_health_check_reads_and_well_inside_its_age_limit():
    config = HeartbeatConfig()

    assert config.path == health.DEFAULT_PATH
    assert config.interval_secs * 3 <= health.DEFAULT_MAX_AGE_S
