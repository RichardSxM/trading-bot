"""covers: spec 0029 AC-8 (the 1 hour seed fetch, BTC's seed to every coin), AC-10 (``PAPER_STRATEGY=six_vote_trend``:
its version records every setting and the coin set, one instance per coin with its own ``order_id_tag``, BTC
required, demo only, ``MakerBarRuleView``).

The fetch is scripted: no request leaves the test.
"""

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trader.main import TraderSettings, seed_fetch
from trader.paper_strategy import (
    SEEDED_STRATEGIES,
    MakerBarRuleView,
    build_paper_strategy,
    paper_version,
    six_vote_trend_config,
)
from trader.warmup import warmup_seed
from trading_core import versioning
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.six_vote_trend import SixVoteTrend
from trading_core.node.strategy import PauseGate
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks

PEPE = "PF_PEPEUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
BOTH = f"{PEPE},{BTC_PERP}"
D = Decimal
HOUR_MS = 3_600_000
NOW_S = int(datetime(2026, 11, 6, 12, 20, 0, tzinfo=UTC).timestamp())
NOW_NS = NOW_S * 1_000_000_000


@pytest.fixture
def six(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "six_vote_trend")


def _settings(monkeypatch, coins=BOTH):
    monkeypatch.setenv("PAPER_INSTRUMENTS", coins)
    return TraderSettings()


def _build(settings, coin, seed=(), btc_seed=()):
    ledger = FundingLedger(D(0))
    return build_paper_strategy(
        settings, uuid4(), coin, PauseGate(), EntryBlocks(), lambda item: None, ledger, seed, btc_seed
    )


def test_the_version_records_every_setting_and_the_coin_set(six, monkeypatch):
    version = paper_version(_settings(monkeypatch))

    assert (version.name, version.kind.value) == ("six_vote_trend", "hour_level")
    expected = {
        "paper_strategy": "six_vote_trend",
        "bar_spec": "1-HOUR-LAST",
        "time_stop_bars": 24,
        "atr_period": 14,
        "atr_floor_mult": "1",
        "entry_offset_atr": "0",
        "entry_wait_bars": 1,
        "exit_wait_bars": 1,
        "atr_min_bps": None,
        "spread_max_atr": "0.1",
        "trail_atr": "3",
        "trail_min_step_atr": "0.1",
        "trail_min_interval_s": 5,
        "trail_max_refusals": 5,
        "btc_instrument_id": BTC_PERP,
        "btc_wait_s": 3,
        "instruments": [PEPE, BTC_PERP],
    }
    assert {key: version.config[key] for key in expected} == expected
    assert any(key.startswith("risk_") for key in version.config)
    assert version.frozen == versioning.freeze(SixVoteTrend.__module__, version.config)


def test_the_version_config_matches_the_built_config(six, monkeypatch):
    settings = _settings(monkeypatch)
    version = paper_version(settings)
    config = six_vote_trend_config(settings, uuid4(), PEPE)

    for key in ("time_stop_bars", "entry_wait_bars", "exit_wait_bars", "trail_min_interval_s", "trail_max_refusals"):
        assert version.config[key] == getattr(config, key)
    for key in ("entry_offset_atr", "spread_max_atr", "trail_atr", "trail_min_step_atr", "atr_floor_mult"):
        assert D(str(version.config[key])) == getattr(config, key)
    assert (version.config["btc_instrument_id"], version.config["btc_wait_s"]) == (
        config.btc_instrument_id,
        config.btc_wait_s,
    )
    assert config.atr_min_bps is None


def test_each_coin_gets_its_own_instance_with_its_seed_and_btc_s(six, monkeypatch):
    settings = _settings(monkeypatch)
    seed = [BarInput(3_600_000_000_000, D(1), D(2), D(1), D(1), D(1))]
    btc_seed = [BarInput(3_600_000_000_000, D(80000), D(80001), D(79999), D(80000), D(1))]

    built = [_build(settings, coin, seed, btc_seed) for coin in settings.instrument_list]

    assert [str(strategy.id) for strategy, _ in built] == ["SixVoteTrend-PEPEUSD", "SixVoteTrend-XBTUSD"]
    assert all(isinstance(view, MakerBarRuleView) for _, view in built)
    pepe, btc = (strategy for strategy, _ in built)
    assert pepe._seed == tuple(seed)
    assert pepe._btc_seed_pending
    assert not pepe._is_btc
    assert btc._is_btc
    assert not btc._btc_seed_pending  # BTC reads its own bars and seed


def test_twelve_coins_build_twelve_instances(six, monkeypatch):
    coins = [BTC_PERP, ETH, PEPE] + [f"PF_C{n}USD.KRAKEN" for n in range(9)]
    settings = _settings(monkeypatch, ",".join(coins))

    built = [_build(settings, coin) for coin in settings.instrument_list]

    assert len({str(strategy.id) for strategy, _ in built}) == 12


def test_a_coin_list_without_btc_is_refused(six, monkeypatch):
    settings = _settings(monkeypatch, f"{PEPE},{ETH}")

    with pytest.raises(ValueError, match=r"needs PF_XBTUSD\.KRAKEN"):
        six_vote_trend_config(settings, uuid4(), PEPE)


def test_it_is_refused_outside_demo(six, monkeypatch):
    settings = _settings(monkeypatch).model_copy(update={"trading_env": "live"})

    with pytest.raises(ValueError, match="live taker fee"):
        six_vote_trend_config(settings, uuid4(), PEPE)


def test_it_takes_the_warmup_seed():
    assert "six_vote_trend" in SEEDED_STRATEGIES


# The 1 hour seed (AC-8) ---------------------------------------------------------------------


def _body(count=320):
    # The last candle opens at 12:00 and is still in progress at 12:20.
    last_open = (NOW_S - 1200) * 1000
    candles = [
        {
            "time": last_open - (count - 1 - i) * HOUR_MS,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "2",
        }
        for i in range(count)
    ]
    return json.dumps({"candles": candles, "more_candles": False}).encode()


class _Get:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def __call__(self, url, timeout_s):
        self.urls.append(url)
        return self.body


def test_the_six_vote_fetch_takes_300_one_hour_candles():
    get = _Get(_body())
    fetch = seed_fetch("six_vote_trend")

    seed = fetch.func(PEPE, lambda: NOW_NS, get, lambda s: None, **fetch.keywords)  # type: ignore[attr-defined]

    assert len(seed) == 300
    assert seed[-1].ts_ns == (NOW_S - 1200) * 1_000_000_000  # the 11:00 candle, closed at 12:00
    assert seed[-2].ts_ns == seed[-1].ts_ns - HOUR_MS * 1_000_000
    [url] = get.urls
    assert url == f"https://futures.kraken.com/api/charts/v1/trade/PF_PEPEUSD/1h?from={NOW_S - 320 * 3600}&to={NOW_S}"


def test_fewer_than_250_complete_one_hour_candles_fail_the_seed(caplog):
    fetch = seed_fetch("six_vote_trend")

    with caplog.at_level(logging.WARNING):
        seed = fetch.func(PEPE, lambda: NOW_NS, _Get(_body(250)), lambda s: None, **fetch.keywords)  # type: ignore[attr-defined]

    assert seed == ()  # 249 complete: the last one is still open
    [failed] = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "warmup_seed_failed"]
    assert failed.fields["reason"] == "too_few"


def test_other_strategies_keep_r2_s_five_minute_fetch():
    assert seed_fetch("donchian_scalp_r2") is warmup_seed
