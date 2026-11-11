"""covers: spec 0032 AC-4 (``trend_daily``'s settings, recorded in its version), AC-5 (the paper wiring: many
coins, demo only, one instance with a view per coin, the daily seed, ``BOOK_STRATEGIES``), AC-6 (the ``book`` flag
in ``trader_state``)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trader.main import BOOK_STRATEGIES, TraderSettings, is_book, seed_fetch
from trader.paper_strategy import (
    SEEDED_STRATEGIES,
    TrendPortfolioView,
    build_trend_daily,
    paper_version,
    trend_daily_config,
)
from trading_core import versioning
from trading_core.node.control import trader_state
from trading_core.node.kraken import BTC_PERP
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_daily import TrendDaily
from trading_core.strategy.blocks import EntryBlocks

ETH = "PF_ETHUSD.KRAKEN"
COINS = f"{BTC_PERP},{ETH}"


@pytest.fixture
def daily(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "trend_daily")
    monkeypatch.setenv("PAPER_INSTRUMENTS", COINS)


def test_the_version_records_every_setting_under_its_own_name(daily):
    settings = TraderSettings()

    version = paper_version(settings)

    assert (version.name, version.kind.value) == ("trend_daily", "trend")
    config = version.config
    assert config["paper_strategy"] == "trend_daily"
    assert config["order_id_tag"] == "TD"
    assert config["bar_spec"] == "1-DAY-LAST"
    assert (config["lookback_days"], config["vol_days"], config["min_history_days"]) == (30, 30, 120)
    assert (config["vol_target"], config["gross_cap"]) == ("0.155", "5")
    assert (config["run_at_utc"], config["rebalance_weekday"]) == ("00:05", 0)
    assert (config["retry_max"], config["retry_wait_s"], config["quote_wait_s"]) == (3, 60, 60)
    assert (config["start_cooldown_s"], config["seed_days"], config["seed_min_days"]) == (3600, 150, 121)
    assert (config["coin_take_profit_usd"], config["coin_stop_loss_usd"], config["uses_stop"]) == (None, None, False)
    assert config["instruments"] == sorted([BTC_PERP, ETH])
    risk = {key for key in config if key.startswith("risk_")}
    assert {"risk_max_order_notional_usd", "risk_max_total_exposure_usd", "risk_max_leverage"} <= risk
    assert version.frozen == versioning.freeze(TrendDaily.__module__, config)


def test_its_version_is_not_trend_portfolios(daily, monkeypatch):
    daily_version = paper_version(TraderSettings())
    monkeypatch.setenv("PAPER_STRATEGY", "trend_portfolio")
    portfolio_version = paper_version(TraderSettings())

    assert daily_version.frozen.code_hash != portfolio_version.frozen.code_hash
    assert daily_version.frozen.frozen_hash != portfolio_version.frozen.frozen_hash


def test_a_cap_change_is_a_new_version(daily, monkeypatch):
    before = paper_version(TraderSettings()).frozen.frozen_hash
    monkeypatch.setenv("RISK_MAX_TOTAL_EXPOSURE_USD", "9000")
    monkeypatch.setenv("RISK_MAX_ORDER_NOTIONAL_USD", "2500")

    assert paper_version(TraderSettings()).frozen.frozen_hash != before


def test_the_config_carries_the_settings_and_the_risk_caps(daily, monkeypatch):
    monkeypatch.setenv("RISK_MAX_TOTAL_EXPOSURE_USD", "9000")
    monkeypatch.setenv("RISK_MAX_ORDER_NOTIONAL_USD", "2500")
    settings = TraderSettings()

    config = trend_daily_config(settings, uuid4())

    assert config.order_id_tag == "TD"
    assert config.instrument_id == BTC_PERP
    assert config.instruments == tuple(sorted([BTC_PERP, ETH]))
    assert (config.vol_target, config.gross_cap) == (Decimal("0.155"), Decimal(5))
    assert (config.coin_take_profit_usd, config.coin_stop_loss_usd) == (None, None)
    assert config.max_order_notional_usd == Decimal(2500)
    assert config.use_market_context is False


def test_it_is_refused_outside_demo(daily):
    settings = TraderSettings().model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        trend_daily_config(settings, uuid4())


def test_it_needs_btc_among_the_coins(daily, monkeypatch):
    monkeypatch.setenv("PAPER_INSTRUMENTS", ETH)

    with pytest.raises(ValueError, match=BTC_PERP):
        trend_daily_config(TraderSettings(), uuid4())


def test_it_builds_one_instance_with_a_view_per_coin(daily):
    settings = TraderSettings()

    strategy, views = build_trend_daily(settings, uuid4(), PauseGate(), EntryBlocks(), lambda item: None, {}, None)

    assert isinstance(strategy, TrendDaily)
    assert len(views) == 2
    assert all(isinstance(view, TrendPortfolioView) for view in views)
    assert all(view.stop_order() is None for view in views)


def test_it_is_a_seeded_book_strategy_on_daily_candles(daily):
    fetch = seed_fetch("trend_daily")

    assert "trend_daily" in SEEDED_STRATEGIES
    assert BOOK_STRATEGIES == ("trend_portfolio", "trend_daily", "trend_vol")
    assert fetch.keywords == {"interval": "1d", "keep_bars": 150, "min_bars": 121}  # type: ignore[attr-defined]


def test_the_portfolio_slot_still_needs_trend_portfolio(daily, monkeypatch):
    monkeypatch.setenv("TRADER_SLOT", "portfolio")

    with pytest.raises(ValidationError, match="trend_portfolio"):
        TraderSettings()


# The book flag (AC-6) ------------------------------------------------------------------------


def test_book_is_true_only_for_a_built_demo_book(daily, monkeypatch):
    settings = TraderSettings()

    assert is_book(settings, paper_built=True)
    assert not is_book(settings, paper_built=False)
    assert not is_book(settings.model_copy(update={"trading_env": "live"}), paper_built=True)
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp_tp")
    assert not is_book(TraderSettings(), paper_built=True)


class _Cache:
    def orders_open(self):
        return []

    def orders_inflight(self):
        return []

    def positions_open(self):
        return []


class _Risk:
    trading_state = 1  # ACTIVE


def _state(**kwargs):
    gate = PauseGate()
    gate.unpause()
    return trader_state(gate, _Risk(), _Cache(), "abc", **kwargs)  # type: ignore[arg-type]


def test_trader_state_reports_book_and_defaults_to_false():
    assert _state(book=True)["book"] is True
    assert _state()["book"] is False
