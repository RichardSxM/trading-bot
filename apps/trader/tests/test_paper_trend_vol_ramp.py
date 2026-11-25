"""covers: spec 0034 AC-9 (``trend_vol_ramp``'s paper wiring: every setting in its own version, the config,
demo only, one instance with a view per coin, the daily seed, ``BOOK_STRATEGIES``) and AC-6 (recorded stops
turned into the node's ``stops``)."""

from decimal import Decimal
from uuid import uuid4

import pytest

from trader import paper_strategy
from trader.main import BOOK_STRATEGIES, TraderSettings, is_book, recorded_stops, seed_fetch
from trader.paper_strategy import (
    SEEDED_STRATEGIES,
    TrendPortfolioView,
    build_trend_vol_ramp,
    paper_version,
    trend_vol_config,
    trend_vol_ramp_config,
)
from trading_core import versioning
from trading_core.node.kraken import BTC_PERP
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_vol_ramp import TrendVolRamp
from trading_core.recording.enums import OrderSide
from trading_core.strategy.blocks import EntryBlocks

ETH = "PF_ETHUSD.KRAKEN"
COINS = f"{BTC_PERP},{ETH}"


@pytest.fixture
def ramp(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "trend_vol_ramp")
    monkeypatch.setenv("PAPER_INSTRUMENTS", COINS)


def test_the_constants_are_trend_vols_plus_the_ramp():
    for name in dir(paper_strategy):
        if name.startswith("TVR_") and name not in ("TVR_RAMP_STEPS", "TVR_STOP_VOLS"):
            assert getattr(paper_strategy, name) == getattr(paper_strategy, "TV_" + name.removeprefix("TVR_")), name
    assert (3, Decimal(8)) == (paper_strategy.TVR_RAMP_STEPS, paper_strategy.TVR_STOP_VOLS)


def test_the_version_records_every_setting_under_its_own_name(ramp):
    version = paper_version(TraderSettings())

    assert (version.name, version.kind.value) == ("trend_vol_ramp", "trend")
    config = version.config
    assert config["paper_strategy"] == "trend_vol_ramp"
    assert config["order_id_tag"] == "TVR"
    assert config["bar_spec"] == "1-DAY-LAST"
    assert (config["lookback_days"], config["vol_days"], config["min_history_days"]) == (30, 30, 120)
    assert (config["vol_target"], config["gross_cap"]) == ("0.155", "5")
    assert (config["book_vol_target"], config["rebalance_band"]) == ("0.5", "0.3")
    assert (config["ramp_steps"], config["stop_vols"], config["stop_check"]) == (3, "8", "hourly")
    assert config["run_at_utc"] == "00:05"
    assert (config["start_cooldown_s"], config["seed_days"], config["seed_min_days"]) == (3600, 150, 121)
    assert (config["coin_take_profit_usd"], config["coin_stop_loss_usd"], config["uses_stop"]) == (None, None, False)
    assert config["instruments"] == sorted([BTC_PERP, ETH])
    assert version.frozen == versioning.freeze(TrendVolRamp.__module__, config)


def test_its_version_is_not_trend_vols(ramp, monkeypatch):
    ramp_version = paper_version(TraderSettings())
    monkeypatch.setenv("PAPER_STRATEGY", "trend_vol")
    vol_version = paper_version(TraderSettings())

    assert ramp_version.frozen.code_hash != vol_version.frozen.code_hash
    assert ramp_version.frozen.frozen_hash != vol_version.frozen.frozen_hash


def test_the_config_is_trend_vols_with_the_ramp_and_its_own_tag(ramp):
    settings = TraderSettings()
    run_id = uuid4()

    config = trend_vol_ramp_config(settings, run_id)
    base = trend_vol_config(settings, run_id)

    assert config.order_id_tag == "TVR"
    assert (config.ramp_steps, config.stop_vols) == (3, Decimal(8))
    for field in ("instruments", "vol_target", "gross_cap", "book_vol_target", "rebalance_band", "run_at_utc"):
        assert getattr(config, field) == getattr(base, field), field


def test_it_is_refused_outside_demo(ramp):
    settings = TraderSettings().model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        trend_vol_ramp_config(settings, uuid4())


def test_it_builds_one_instance_with_a_view_per_coin_and_the_stops(ramp):
    strategy, views = build_trend_vol_ramp(
        TraderSettings(), uuid4(), PauseGate(), EntryBlocks(), lambda item: None, {}, None, {BTC_PERP: (1, -1)}
    )

    assert isinstance(strategy, TrendVolRamp)
    assert strategy.stopped[BTC_PERP][0] == -1
    assert len(views) == 2
    assert all(isinstance(view, TrendPortfolioView) for view in views)


def test_it_is_a_seeded_book_strategy_on_daily_candles(ramp):
    fetch = seed_fetch("trend_vol_ramp")

    assert "trend_vol_ramp" in SEEDED_STRATEGIES
    assert "trend_vol_ramp" in BOOK_STRATEGIES
    assert fetch.keywords == {"interval": "1d", "keep_bars": 150, "min_bars": 121}  # type: ignore[attr-defined]
    assert is_book(TraderSettings(), paper_built=True)


def test_a_sell_close_stopped_a_long_and_a_buy_close_a_short():
    assert recorded_stops({BTC_PERP: (5, OrderSide.SELL), ETH: (7, OrderSide.BUY)}) == {
        BTC_PERP: (5, 1),
        ETH: (7, -1),
    }
