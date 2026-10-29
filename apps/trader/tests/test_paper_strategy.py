"""covers: ``PAPER_STRATEGY`` picks the demo paper strategy (``placeholder_timed`` by default, ``donchian_4h``
at the ``donchian_4h_pair`` study's chosen point, or ``donchian_scalp``, that point on 5 minute bars), with its
own strategy version."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trader.main import TraderSettings
from trader.paper_strategy import (
    BarRuleView,
    build_paper_strategy,
    donchian_config,
    donchian_scalp_config,
    paper_version,
)
from trading_core import versioning
from trading_core.node.donchian_4h import Donchian4h
from trading_core.node.donchian_scalp import DonchianScalp
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.placeholder import PlaceholderTimed
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")


def _build(settings):
    return build_paper_strategy(
        settings, uuid4(), BTC_PERP, PauseGate(), EntryBlocks(), lambda item: None, FundingLedger(Decimal(0))
    )


def test_the_default_is_the_placeholder_with_its_old_version(demo):
    settings = TraderSettings()

    version = paper_version(settings)
    strategy, view = _build(settings)

    assert settings.paper_strategy == "placeholder_timed"
    assert version.name == "placeholder_timed"
    assert version.kind.value == "scalping"
    assert version.config == settings.strategy_config()
    assert version.frozen == versioning.freeze(PlaceholderTimed.__module__, settings.strategy_config())
    assert isinstance(strategy, PlaceholderTimed)
    assert view is strategy


def test_donchian_4h_runs_the_chosen_grid_point(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_4h")
    settings = TraderSettings()

    version = paper_version(settings)
    strategy, view = _build(settings)

    assert version.name == "donchian_4h"
    assert version.kind.value == "hour_level"
    assert version.config["entry_bars"] == 20
    assert version.config["stop_atr"] == "3"
    assert not any(name.startswith("placeholder_") for name in version.config)
    assert version.config["risk_per_trade"] == str(settings.risk_per_trade)
    assert version.frozen == versioning.freeze(Donchian4h.__module__, version.config)
    assert isinstance(strategy, Donchian4h)
    assert isinstance(view, BarRuleView)
    assert str(strategy.bar_type) == f"{BTC_PERP}-4-HOUR-LAST-INTERNAL"


def test_donchian_scalp_runs_the_same_point_on_5_minute_bars(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_4h")
    four_hour = paper_version(TraderSettings())
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp")
    settings = TraderSettings()

    version = paper_version(settings)
    strategy, view = _build(settings)

    assert version.name == "donchian_scalp"
    assert version.kind.value == "scalping"
    assert version.config["bar_spec"] == "5-MINUTE-LAST"
    assert (version.config["entry_bars"], version.config["stop_atr"]) == (20, "3")
    assert version.frozen == versioning.freeze(DonchianScalp.__module__, version.config)
    assert version.frozen.code_hash != four_hour.frozen.code_hash
    assert isinstance(strategy, DonchianScalp)
    assert isinstance(view, BarRuleView)
    assert str(strategy.bar_type) == f"{BTC_PERP}-5-MINUTE-LAST-INTERNAL"


def test_donchian_scalp_is_refused_outside_demo(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp")
    settings = TraderSettings().model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        donchian_scalp_config(settings, uuid4(), BTC_PERP)


def test_donchian_sizes_with_the_sandbox_fee_and_the_risk_settings(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_4h")
    settings = TraderSettings()

    config = donchian_config(settings, uuid4(), BTC_PERP)

    assert config.taker_fee == settings.sandbox_taker_fee
    assert config.max_order_notional_usd == settings.risk_max_order_notional_usd
    assert config.stale_quote_s == settings.risk_stale_quote_s
    assert (config.entry_bars, config.stop_atr, config.time_stop_bars) == (20, Decimal(3), 60)


def test_placeholder_settings_do_not_change_the_donchian_version(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_4h")
    before = paper_version(TraderSettings())
    monkeypatch.setenv("PLACEHOLDER_STOP_PCT", "0.5")

    assert paper_version(TraderSettings()).frozen == before.frozen


def test_an_unknown_paper_strategy_is_refused(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "vwap_retest")

    with pytest.raises(ValidationError, match="paper_strategy"):
        TraderSettings()


def test_donchian_is_refused_outside_demo(demo, monkeypatch):
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_4h")
    settings = TraderSettings().model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        donchian_config(settings, uuid4(), BTC_PERP)
