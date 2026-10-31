"""covers: spec 0026 AC-16 (``PAPER_STRATEGY=donchian_scalp_r2``: its version records every setting and the coin
set, one r2 per coin with its own ``order_id_tag``, the seed, the demo only config, and ``MakerBarRuleView``)."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trader.main import TraderSettings
from trader.paper_strategy import (
    SEEDED_STRATEGIES,
    MakerBarRuleView,
    build_paper_strategy,
    donchian_scalp_r2_config,
    paper_version,
)
from trading_core import versioning
from trading_core.node.bar_rule_maker import Phase
from trading_core.node.donchian_scalp_r2 import DonchianScalpR2
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.strategy import PauseGate
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks

PEPE = "PF_PEPEUSD.KRAKEN"
BOTH = f"{PEPE},{BTC_PERP}"
D = Decimal


@pytest.fixture
def r2(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp_r2")


def _settings(monkeypatch, coins=BTC_PERP):
    monkeypatch.setenv("PAPER_INSTRUMENTS", coins)
    return TraderSettings()


def _build(settings, coin=BTC_PERP, seed=()):
    ledger = FundingLedger(D(0))
    return build_paper_strategy(settings, uuid4(), coin, PauseGate(), EntryBlocks(), lambda item: None, ledger, seed)


def test_r2_s_version_records_every_setting_and_the_coin_set(r2, monkeypatch):
    settings = _settings(monkeypatch, BOTH)
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp_r1")
    r1 = paper_version(TraderSettings())

    version = paper_version(settings)

    assert (version.name, version.kind.value) == ("donchian_scalp_r2", "scalping")
    assert {
        key: version.config[key]
        for key in (
            "ema_period",
            "entry_offset_atr",
            "entry_wait_bars",
            "exit_wait_bars",
            "atr_min_bps",
            "spread_max_atr",
            "instruments",
        )
    } == {
        "ema_period": 200,
        "entry_offset_atr": "0.5",
        "entry_wait_bars": 3,
        "exit_wait_bars": 3,
        "atr_min_bps": "10",
        "spread_max_atr": "0.1",
        "instruments": [PEPE, BTC_PERP],
    }
    shared = {key: value for key, value in r1.config.items() if key != "paper_strategy"}
    assert {key: version.config[key] for key in shared} == shared
    assert version.config["paper_strategy"] == "donchian_scalp_r2"
    assert version.frozen == versioning.freeze(DonchianScalpR2.__module__, version.config)
    assert version.frozen.code_hash != r1.frozen.code_hash


def test_each_coin_gets_its_own_r2_with_the_seed(r2, monkeypatch):
    settings = _settings(monkeypatch, BOTH)
    seed = [BarInput(300_000_000_000, D(1), D(2), D(1), D(1), D(1))]

    built = [_build(settings, coin, seed) for coin in settings.instrument_list]

    assert [str(strategy.id) for strategy, _ in built] == ["DonchianScalpR2-PEPEUSD", "DonchianScalpR2-XBTUSD"]
    assert all(isinstance(strategy, DonchianScalpR2) for strategy, _ in built)
    assert all(isinstance(view, MakerBarRuleView) for _, view in built)
    assert [strategy._seed for strategy, _ in built] == [tuple(seed), tuple(seed)]


def test_r2_builds_the_demo_config(r2, monkeypatch):
    settings = _settings(monkeypatch)

    config = donchian_scalp_r2_config(settings, uuid4(), BTC_PERP)

    assert (config.entry_bars, config.stop_atr, config.time_stop_bars, config.ema_period) == (20, D(3), 60, 200)
    assert (config.entry_offset_atr, config.entry_wait_bars, config.exit_wait_bars) == (D("0.5"), 3, 3)
    assert (config.atr_min_bps, config.spread_max_atr) == (D(10), D("0.1"))
    assert config.taker_fee == settings.sandbox_taker_fee
    assert config.order_id_tag == "XBTUSD"


def test_r2_is_refused_outside_demo(r2, monkeypatch):
    settings = _settings(monkeypatch).model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        donchian_scalp_r2_config(settings, uuid4(), BTC_PERP)


def test_r1_and_r2_take_the_warmup_seed():
    assert set(SEEDED_STRATEGIES) == {"donchian_scalp_r1", "donchian_scalp_r2"}


@pytest.mark.parametrize(("phase", "exiting"), [(Phase.EXITING, True), (Phase.PROTECTED, False), (Phase.FLAT, False)])
def test_the_view_reports_exiting_while_an_exit_works(phase, exiting):
    view = MakerBarRuleView(SimpleNamespace(phase=phase))  # type: ignore[arg-type]

    assert view.exiting is exiting
    assert view.exit_due_ns is None
