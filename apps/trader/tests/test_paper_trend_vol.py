"""covers: ``trend_vol``'s paper wiring: its settings recorded in its own version, the config, demo only, one
instance with a view per coin, the daily seed, and the ``book`` flag."""

from decimal import Decimal
from uuid import uuid4

import pytest

from trader.main import BOOK_STRATEGIES, TraderSettings, is_book, seed_fetch
from trader.paper_strategy import (
    SEEDED_STRATEGIES,
    TrendPortfolioView,
    build_trend_vol,
    paper_version,
    trend_vol_config,
)
from trading_core import versioning
from trading_core.node.kraken import BTC_PERP
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_vol import TrendVol
from trading_core.strategy.blocks import EntryBlocks

ETH = "PF_ETHUSD.KRAKEN"
COINS = f"{BTC_PERP},{ETH}"


@pytest.fixture
def vol(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "trend_vol")
    monkeypatch.setenv("PAPER_INSTRUMENTS", COINS)


def test_the_version_records_every_setting_under_its_own_name(vol):
    version = paper_version(TraderSettings())

    assert (version.name, version.kind.value) == ("trend_vol", "trend")
    config = version.config
    assert config["paper_strategy"] == "trend_vol"
    assert config["order_id_tag"] == "TV"
    assert config["bar_spec"] == "1-DAY-LAST"
    assert (config["lookback_days"], config["vol_days"], config["min_history_days"]) == (30, 30, 120)
    assert (config["vol_target"], config["gross_cap"]) == ("0.155", "5")
    assert (config["book_vol_target"], config["rebalance_band"]) == ("0.5", "0.3")
    assert config["run_at_utc"] == "00:05"
    assert (config["start_cooldown_s"], config["seed_days"], config["seed_min_days"]) == (3600, 150, 121)
    assert (config["coin_take_profit_usd"], config["coin_stop_loss_usd"], config["uses_stop"]) == (None, None, False)
    assert config["instruments"] == sorted([BTC_PERP, ETH])
    assert version.frozen == versioning.freeze(TrendVol.__module__, config)


def test_its_version_is_not_trend_dailys(vol, monkeypatch):
    vol_version = paper_version(TraderSettings())
    monkeypatch.setenv("PAPER_STRATEGY", "trend_daily")
    daily_version = paper_version(TraderSettings())

    assert vol_version.frozen.code_hash != daily_version.frozen.code_hash
    assert vol_version.frozen.frozen_hash != daily_version.frozen.frozen_hash


def test_the_config_carries_the_book_settings_and_the_risk_caps(vol, monkeypatch):
    monkeypatch.setenv("RISK_MAX_TOTAL_EXPOSURE_USD", "9000")
    monkeypatch.setenv("RISK_MAX_ORDER_NOTIONAL_USD", "2500")

    config = trend_vol_config(TraderSettings(), uuid4())

    assert config.order_id_tag == "TV"
    assert config.instruments == tuple(sorted([BTC_PERP, ETH]))
    assert (config.vol_target, config.gross_cap) == (Decimal("0.155"), Decimal(5))
    assert (config.book_vol_target, config.rebalance_band) == (Decimal("0.5"), Decimal("0.3"))
    assert config.max_order_notional_usd == Decimal(2500)


def test_it_is_refused_outside_demo(vol):
    settings = TraderSettings().model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        trend_vol_config(settings, uuid4())


def test_it_builds_one_instance_with_a_view_per_coin(vol):
    strategy, views = build_trend_vol(
        TraderSettings(), uuid4(), PauseGate(), EntryBlocks(), lambda item: None, {}, None
    )

    assert isinstance(strategy, TrendVol)
    assert len(views) == 2
    assert all(isinstance(view, TrendPortfolioView) for view in views)


def test_it_is_a_seeded_book_strategy_on_daily_candles(vol):
    fetch = seed_fetch("trend_vol")

    assert "trend_vol" in SEEDED_STRATEGIES
    assert "trend_vol" in BOOK_STRATEGIES
    assert fetch.keywords == {"interval": "1d", "keep_bars": 150, "min_bars": 121}  # type: ignore[attr-defined]
    assert is_book(TraderSettings(), paper_built=True)
