"""covers: spec 0025 AC-15 (``PAPER_INSTRUMENTS`` and the instrument cache check), AC-16 (one r1 per coin with its
own ``order_id_tag``), AC-17 (the coin set in r1's version), AC-19 (a per order cap per coin, the per coin blocks)."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from trader.coin_blocks import coin_blocks
from trader.instrument_check import missing_instruments
from trader.main import TraderSettings, build_config
from trader.paper_strategy import build_paper_strategy, donchian_scalp_r1_config, order_id_tag, paper_version
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import BlockReason, EntryBlocks

PEPE = "PF_PEPEUSD.KRAKEN"
BOTH = f"{PEPE},{BTC_PERP}"
SECRETS = {"redis_password": SecretStr("r" * 32), "postgres_password": SecretStr("p" * 32)}


@pytest.fixture
def r1(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp_r1")


def _settings(monkeypatch, coins):
    monkeypatch.setenv("PAPER_INSTRUMENTS", coins)
    return TraderSettings()


# AC-15 --------------------------------------------------------------------------------------


def test_the_default_is_btc_only(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.delenv("PAPER_INSTRUMENTS", raising=False)

    assert TraderSettings().instrument_list == [BTC_PERP]


def test_the_order_you_write_the_coins_in_changes_nothing(r1, monkeypatch):
    first = _settings(monkeypatch, BOTH)
    second = _settings(monkeypatch, f"{BTC_PERP} , {PEPE}")

    assert first.instrument_list == second.instrument_list == [PEPE, BTC_PERP]
    assert paper_version(first).frozen == paper_version(second).frozen


@pytest.mark.parametrize(
    ("coins", "message"),
    [
        (f"{BTC_PERP},{BTC_PERP}", "duplicate"),
        (f"{BTC_PERP},", "PF_<SYMBOL>.KRAKEN"),
        ("", "PF_<SYMBOL>.KRAKEN"),
        ("BTCUSD", "PF_<SYMBOL>.KRAKEN"),
        ("PF_XBTUSD.BINANCE", "PF_<SYMBOL>.KRAKEN"),
    ],
)
def test_a_bad_coin_list_refuses_to_start(r1, monkeypatch, coins, message):
    with pytest.raises(ValidationError, match=message):
        _settings(monkeypatch, coins)


def test_two_coins_need_r1(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp")

    with pytest.raises(ValidationError, match="needs donchian_scalp_r1"):
        _settings(monkeypatch, BOTH)


def test_a_coin_missing_from_the_instrument_cache_is_named():
    loaded = {BTC_PERP}

    assert missing_instruments([PEPE, BTC_PERP], lambda i: i.value in loaded) == [PEPE]
    assert missing_instruments([BTC_PERP], lambda i: i.value in loaded) == []


# AC-16 --------------------------------------------------------------------------------------


def test_each_coin_gets_its_own_r1_and_strategy_id(r1, monkeypatch):
    settings = _settings(monkeypatch, BOTH)
    run_id, gate, blocks = uuid4(), PauseGate(), EntryBlocks()

    built = [
        build_paper_strategy(settings, run_id, coin, gate, blocks, lambda item: None, FundingLedger(Decimal(0)))
        for coin in settings.instrument_list
    ]

    assert [order_id_tag(coin) for coin in settings.instrument_list] == ["PEPEUSD", "XBTUSD"]
    assert [str(strategy.id) for strategy, _ in built] == ["DonchianScalpR1-PEPEUSD", "DonchianScalpR1-XBTUSD"]
    assert [str(strategy.bar_type) for strategy, _ in built] == [
        f"{PEPE}-5-MINUTE-LAST-INTERNAL",
        f"{BTC_PERP}-5-MINUTE-LAST-INTERNAL",
    ]


# AC-17 --------------------------------------------------------------------------------------


def test_the_coin_set_is_in_r1s_version_and_nowhere_else(r1, monkeypatch):
    one = paper_version(_settings(monkeypatch, BTC_PERP))
    two = paper_version(_settings(monkeypatch, BOTH))

    assert one.config["instruments"] == [BTC_PERP]
    assert two.config["instruments"] == [PEPE, BTC_PERP]
    assert one.frozen.frozen_hash != two.frozen.frozen_hash
    assert one.frozen.code_hash == two.frozen.code_hash
    assert "order_id_tag" not in two.config
    config = donchian_scalp_r1_config(_settings(monkeypatch, BOTH), uuid4(), PEPE)
    assert config.order_id_tag == "PEPEUSD"


def test_other_strategies_keep_their_version(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("PAPER_STRATEGY", "donchian_scalp")
    monkeypatch.delenv("PAPER_INSTRUMENTS", raising=False)
    before = paper_version(TraderSettings())

    after = paper_version(_settings(monkeypatch, BTC_PERP))

    assert "instruments" not in after.config
    assert after.frozen == before.frozen


# AC-19 --------------------------------------------------------------------------------------


def test_every_coin_has_a_per_order_cap(r1, monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    settings = _settings(monkeypatch, BOTH).model_copy(
        update={"redis_host": "redis", "kraken_key_rotated_on": date(2026, 9, 1)}
    )

    caps = build_config(settings, SECRETS).risk_engine.max_notional_per_order

    cap = str(settings.risk_max_order_notional_usd)
    assert {str(k): str(v) for k, v in caps.items()} == {BTC_PERP: cap, PEPE: cap}


def test_a_fresh_coin_never_clears_a_stale_coins_block():
    shared = EntryBlocks()
    blocks = coin_blocks(shared, [PEPE, BTC_PERP])
    events = []

    for _ in range(10):  # each coin's stale check, once a second
        if blocks[PEPE].add(BlockReason.STALE_FEED):
            events.append(PEPE)  # the stale check records one risk event when add is True
        blocks[BTC_PERP].clear(BlockReason.STALE_FEED)
        assert shared.active() == (BlockReason.STALE_FEED,)

    assert events == [PEPE]
    assert blocks[PEPE].clear(BlockReason.STALE_FEED) is True
    assert shared.active() == ()


def test_the_block_clears_only_when_every_coin_is_fresh():
    shared = EntryBlocks()
    blocks = coin_blocks(shared, [PEPE, BTC_PERP])

    assert blocks[PEPE].add(BlockReason.STALE_FEED) is True
    assert blocks[BTC_PERP].add(BlockReason.STALE_FEED) is True
    assert blocks[PEPE].clear(BlockReason.STALE_FEED) is True
    assert shared.active() == (BlockReason.STALE_FEED,)
    assert blocks[BTC_PERP].clear(BlockReason.STALE_FEED) is True
    assert shared.active() == ()
    assert blocks[BTC_PERP].clear(BlockReason.STALE_FEED) is False


def test_other_reasons_go_straight_to_the_shared_blocks():
    shared = EntryBlocks()
    blocks = coin_blocks(shared, [PEPE, BTC_PERP])
    heard = []
    blocks[PEPE].subscribe(lambda reason, active: heard.append((reason, active)))

    assert blocks[BTC_PERP].add(BlockReason.STOP_NOT_ACCEPTED) is True
    assert blocks[PEPE].add(BlockReason.STOP_NOT_ACCEPTED) is False
    assert blocks[PEPE].active() == shared.active() == (BlockReason.STOP_NOT_ACCEPTED,)
    assert blocks[PEPE].since() == shared.since()
    assert blocks[BTC_PERP].add(BlockReason.TRADE_CAP) is True
    assert blocks[PEPE].clear(BlockReason.TRADE_CAP) is True
    assert heard == [
        (BlockReason.STOP_NOT_ACCEPTED, True),
        (BlockReason.TRADE_CAP, True),
        (BlockReason.TRADE_CAP, False),
    ]
    with pytest.raises(ValueError, match="holds until restart"):
        blocks[PEPE].clear(BlockReason.STOP_NOT_ACCEPTED)
