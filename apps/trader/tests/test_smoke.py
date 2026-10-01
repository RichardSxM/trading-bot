import asyncio
from datetime import date
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from trader.main import TraderSettings, build_config
from trading_core.boot import Boot
from trading_core.naming import NodeIdMismatch
from trading_core.settings import Settings, TradingEnv

SECRETS = {
    "redis_password": SecretStr("r" * 32),
    "postgres_password": SecretStr("p" * 32),
    "kraken_api_key": SecretStr("k" * 32),
    "kraken_api_secret": SecretStr("s" * 32),
}


def _settings(env: TradingEnv) -> TraderSettings:
    return TraderSettings(trading_env=env, redis_host="redis", redis_port=6379, kraken_key_rotated_on=date(2026, 9, 1))


def test_demo_config_uses_env_tagged_id_and_stable_redis_cache(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"), SECRETS)

    assert config.trader_id.value == "TRADER-DEMO"
    assert config.cache is not None
    assert config.cache.database is not None
    assert config.cache.database.type == "redis"
    assert config.cache.use_trader_prefix is True
    assert config.cache.use_instance_id is False
    assert config.cache.flush_on_start is False
    assert "KRAKEN" in config.data_clients
    assert config.exec_clients == {}


def test_explicit_trader_id_must_match_env(monkeypatch):
    monkeypatch.setenv("TRADER_ID", "TRADER-LIVE")
    with pytest.raises(NodeIdMismatch):
        build_config(_settings("demo"), SECRETS)


def test_data_client_reads_live_public_feeds_in_demo(monkeypatch):
    from nautilus_trader.adapters.kraken import KrakenEnvironment

    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"), SECRETS)
    assert config.data_clients["KRAKEN"].environment == KrakenEnvironment.LIVE


def test_config_guard_passes_a_demo_node_without_execution_clients(monkeypatch):
    # The Kraken key variables are refused by boot for every process (core test_env_guard).
    from trader.main import guard

    monkeypatch.delenv("TRADER_ID", raising=False)
    guard(_settings("demo"), SECRETS)


def test_live_config_uses_the_live_tag(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    assert build_config(_settings("live"), SECRETS).trader_id.value == "TRADER-LIVE"


def test_explicit_trader_id_with_matching_tag_is_used(monkeypatch):
    monkeypatch.setenv("TRADER_ID", "TRADER-demo")
    assert build_config(_settings("demo"), SECRETS).trader_id.value == "TRADER-demo"


def test_cache_points_at_the_configured_redis(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    settings = Settings(trading_env="demo", redis_host="cache.internal", redis_port=6380)

    cache = build_config(settings, SECRETS).cache
    assert cache is not None
    database = cache.database
    assert database is not None

    assert (database.host, database.port) == ("cache.internal", 6380)


def test_cache_logs_in_as_the_services_redis_user(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    settings = Settings(trading_env="demo", redis_username="trader")

    cache = build_config(settings, SECRETS).cache
    assert cache is not None
    database = cache.database
    assert database is not None

    assert (database.username, database.password) == ("trader", "r" * 32)


class _FakeNode:
    instances: ClassVar[list[Any]] = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.actors = []
        _FakeNode.instances.append(self)

    @property
    def trader(self):
        return self

    def add_actor(self, actor):
        self.actors.append(actor)
        self.calls.append(("add_actor", type(actor).__name__))

    def add_data_client_factory(self, name, factory):
        self.calls.append(("factory", name))

    def add_exec_client_factory(self, name, factory):
        self.calls.append(("exec_factory", name))

    def build(self):
        self.calls.append("build")

    def run(self):
        self.calls.append("run")
        if getattr(self, "fail_run", False):
            raise RuntimeError("engine crashed")

    def dispose(self):
        self.calls.append("dispose")

    # The live key check runs as a task on the node's loop.
    cache = None

    def get_event_loop(self):
        import asyncio

        if not hasattr(self, "_loop"):
            self._loop = asyncio.new_event_loop()
        return self._loop

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def fake_main(monkeypatch):
    import trader.main as trader_main

    _FakeNode.instances = []
    monkeypatch.delenv("TRADER_ID", raising=False)
    monkeypatch.setattr(trader_main, "boot", lambda kind, **kw: Boot(_settings("demo"), SECRETS))
    monkeypatch.setattr(trader_main, "TradingNode", _FakeNode)
    return trader_main


def test_main_refuses_to_start_when_redis_is_unreachable(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: False)

    with pytest.raises(SystemExit) as exit_info:
        fake_main.main()

    assert exit_info.value.code == 1
    assert _FakeNode.instances == []


def test_main_builds_and_runs_the_node_with_a_heartbeat(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)

    fake_main.main()

    (node,) = _FakeNode.instances
    assert node.config.trader_id.value == "TRADER-DEMO"
    assert ("add_actor", "HeartbeatActor") in node.calls
    assert ("factory", "KRAKEN") in node.calls
    assert node.calls[-3:] == ["build", "run", "dispose"]


def test_main_disposes_the_node_even_when_run_fails(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)
    monkeypatch.setattr(_FakeNode, "fail_run", True, raising=False)

    with pytest.raises(RuntimeError):
        fake_main.main()

    assert _FakeNode.instances[0].calls[-1] == "dispose"


# spec 0003: the live Kraken path (AC-7, AC-8, AC-9)


def test_live_config_adds_the_kraken_exec_client_with_the_file_keys(monkeypatch):
    from nautilus_trader.adapters.kraken import KrakenEnvironment

    monkeypatch.delenv("TRADER_ID", raising=False)
    client = build_config(_settings("live"), SECRETS).exec_clients["KRAKEN"]

    assert (client.api_key, client.api_secret) == ("k" * 32, "s" * 32)
    assert client.environment == KrakenEnvironment.LIVE
    assert client.base_url_http_futures is None


def test_live_config_can_point_kraken_at_other_urls(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    client = build_config(_settings("live"), SECRETS, ("http://127.0.0.1:9", "ws://127.0.0.1:9")).exec_clients["KRAKEN"]

    assert (client.base_url_http_futures, client.base_url_ws_futures) == ("http://127.0.0.1:9", "ws://127.0.0.1:9")


def test_live_trader_is_refused_until_kraken_joins_the_allowlist(monkeypatch):
    from trader.main import guard
    from trading_core.env_guard import ALLOWED_EXEC_CLIENTS, EnvGuardRefused

    monkeypatch.delenv("TRADER_ID", raising=False)
    with pytest.raises(EnvGuardRefused, match="KRAKEN"):
        guard(_settings("live"), SECRETS)
    allowlist = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}
    guard(_settings("live"), SECRETS, allowlist)


def test_trader_loads_the_kraken_files_only_in_live():
    from trader.main import trader_secrets

    assert trader_secrets(_settings("demo")) == ("postgres_password", "redis_password")
    assert trader_secrets(_settings("live")) == (
        "postgres_password",
        "redis_password",
        "kraken_api_key",
        "kraken_api_secret",
    )


def test_rotation_date_is_required_in_live_only(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("TRADING_ENV", "live")
    with pytest.raises(ValidationError, match="KRAKEN_KEY_ROTATED_ON is required"):
        TraderSettings()
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert TraderSettings().kraken_key_rotated_on is None


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_rotation_date_counts_as_unset(monkeypatch, value):
    # covers: AC-9 (compose.live.yaml passes ${KRAKEN_KEY_ROTATED_ON:-}, empty when unset)
    from pydantic import ValidationError

    monkeypatch.setenv("KRAKEN_KEY_ROTATED_ON", value)
    monkeypatch.setenv("TRADING_ENV", "live")
    with pytest.raises(ValidationError, match="KRAKEN_KEY_ROTATED_ON is required"):
        TraderSettings()
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert TraderSettings().kraken_key_rotated_on is None


@pytest.mark.parametrize(("value", "match"), [("2026-13-01", "date"), ("2999-01-01", "in the future")])
def test_rotation_date_must_be_a_past_iso_date(monkeypatch, value, match):
    from pydantic import ValidationError

    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("KRAKEN_KEY_ROTATED_ON", value)
    with pytest.raises(ValidationError, match=match):
        TraderSettings()


def test_key_check_timeout_defaults_to_60_seconds(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert TraderSettings().kraken_key_check_timeout_s == 60.0


def test_live_run_adds_the_key_age_actor_and_exec_factory_and_a_failed_check_exits_1(fake_main):
    class NoAccountNode(_FakeNode):
        class cache:  # stands in for the Nautilus cache: no Kraken account, ever
            @staticmethod
            def account_for_venue(venue):
                return None

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.3))

    original = fake_main.TradingNode
    fake_main.TradingNode = NoAccountNode
    try:
        code = fake_main.run_node(build_config(_settings("live"), SECRETS), _settings("live"), 0.1, fail_backoff_s=0)
    finally:
        fake_main.TradingNode = original

    (node,) = _FakeNode.instances
    assert code == 1
    assert "stop" in node.calls
    assert ("add_actor", "KeyAgeActor") in node.calls
    assert ("exec_factory", "KRAKEN") in node.calls
    assert node.calls[-1] == "dispose"


def test_demo_run_has_no_key_actors_and_exits_0(fake_main):
    code = fake_main.run_node(build_config(_settings("demo"), SECRETS), _settings("demo"), 2.0)

    (node,) = _FakeNode.instances
    assert code == 0
    assert ("add_actor", "KeyAgeActor") not in node.calls
    assert ("exec_factory", "KRAKEN") not in node.calls


def _cache_with_kraken_account(ts_init):
    from nautilus_trader.accounting.factory import AccountFactory
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType
    from nautilus_trader.model.events import AccountState
    from nautilus_trader.model.identifiers import AccountId
    from nautilus_trader.model.objects import AccountBalance, Money

    state = AccountState(
        account_id=AccountId("KRAKEN-001"),
        account_type=AccountType.MARGIN,
        base_currency=USD,
        reported=True,
        balances=[AccountBalance(Money(100, USD), Money(0, USD), Money(100, USD))],
        margins=[],
        info={},
        event_id=UUID4(),
        ts_event=ts_init,
        ts_init=ts_init,
    )
    cache = Cache()
    cache.add_account(AccountFactory.create(state))
    return cache


@pytest.mark.parametrize(("ts_init", "confirmed"), [(999, False), (1000, True), (1001, True)])
def test_only_an_account_state_from_this_run_confirms_the_key(ts_init, confirmed):
    # covers: AC-8. A restart reloads the last run's account from Redis; it must not count.
    from trader.main import kraken_account_confirmed

    assert kraken_account_confirmed(_cache_with_kraken_account(ts_init), since_ns=1000) is confirmed


def test_no_kraken_account_does_not_confirm_the_key():
    from nautilus_trader.cache.cache import Cache

    from trader.main import kraken_account_confirmed

    assert kraken_account_confirmed(Cache(), since_ns=0) is False


def test_an_account_reloaded_from_a_previous_run_still_fails_the_check(fake_main):
    # covers: AC-8. The revoked key on restart case from the review of 2026-10-01.
    class StaleAccountNode(_FakeNode):
        cache = _cache_with_kraken_account(ts_init=0)

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.3))

    original = fake_main.TradingNode
    fake_main.TradingNode = StaleAccountNode
    try:
        code = fake_main.run_node(build_config(_settings("live"), SECRETS), _settings("live"), 0.1, fail_backoff_s=0)
    finally:
        fake_main.TradingNode = original

    assert code == 1
    assert "stop" in _FakeNode.instances[0].calls


def test_a_crashing_key_check_stops_the_node(fake_main):
    # covers: AC-8, fails closed. The fire and forget finding from the review of 2026-10-01.
    class BrokenCacheNode(_FakeNode):
        cache = None  # account_for_venue raises AttributeError inside the check

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.3))

    original = fake_main.TradingNode
    fake_main.TradingNode = BrokenCacheNode
    try:
        code = fake_main.run_node(build_config(_settings("live"), SECRETS), _settings("live"), 5.0, fail_backoff_s=0)
    finally:
        fake_main.TradingNode = original

    assert code == 1
    assert "stop" in _FakeNode.instances[0].calls
