"""covers: spec 0006 AC-2 and AC-10, the sandbox config and the fees it sets on every instrument.

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image.
"""

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.test_kit.providers import TestInstrumentProvider

from trading_core.node.sandbox import FeeSandboxConfig, sandbox_exec_client, with_fees
from trading_core.settings import PaperLoopSettings


def test_with_fees_changes_only_the_fees():
    instrument = TestInstrumentProvider.btcusdt_perp_binance()

    patched = with_fees(instrument, Decimal("0.0002"), Decimal("0.0005"))

    assert (patched.maker_fee, patched.taker_fee) == (Decimal("0.0002"), Decimal("0.0005"))
    assert patched.id == instrument.id
    assert patched.price_increment == instrument.price_increment
    assert patched.size_increment == instrument.size_increment
    assert (patched.ts_event, patched.ts_init) == (instrument.ts_event, instrument.ts_init)


def test_sandbox_config_is_a_usd_margin_netting_account_filling_on_quotes():
    settings = PaperLoopSettings(trading_env="demo")

    config = sandbox_exec_client(settings)

    assert isinstance(config, FeeSandboxConfig)
    assert config.venue == "KRAKEN"
    assert config.starting_balances == ["1000 USD"]
    assert (config.oms_type, config.account_type, config.base_currency) == ("NETTING", "MARGIN", "USD")
    assert config.default_leverage == Decimal(2)
    assert (config.bar_execution, config.trade_execution) == (False, False)
    assert (config.maker_fee, config.taker_fee) == (Decimal("0.0002"), Decimal("0.0005"))
    # A stop whose trigger is already in the market is rejected, so AC-7 handles it.
    assert config.reject_stop_orders is True
