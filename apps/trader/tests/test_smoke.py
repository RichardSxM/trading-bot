import pytest

from trader.main import build_config
from trading_core.naming import NodeIdMismatch
from trading_core.settings import Settings


def _settings(env: str) -> Settings:
    return Settings(trading_env=env, redis_host="redis", redis_port=6379)


def test_demo_config_uses_env_tagged_id_and_stable_redis_cache(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"))

    assert config.trader_id.value == "TRADER-DEMO"
    assert config.cache.database.type == "redis"
    assert config.cache.use_trader_prefix is True
    assert config.cache.use_instance_id is False
    assert config.cache.flush_on_start is False
    assert "KRAKEN" in config.data_clients
    assert config.exec_clients == {}


def test_explicit_trader_id_must_match_env(monkeypatch):
    monkeypatch.setenv("TRADER_ID", "TRADER-LIVE")
    with pytest.raises(NodeIdMismatch):
        build_config(_settings("demo"))


def test_data_client_reads_live_public_feeds_in_demo(monkeypatch):
    from nautilus_trader.adapters.kraken import KrakenEnvironment

    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"))
    assert config.data_clients["KRAKEN"].environment == KrakenEnvironment.LIVE


def test_env_guard_refuses_kraken_keys_in_demo(monkeypatch):
    from trader.main import guard
    from trading_core.env_guard import EnvGuardRefused

    monkeypatch.delenv("TRADER_ID", raising=False)
    guard(_settings("demo"))  # clean environment passes
    monkeypatch.setenv("KRAKEN_FUTURES_API_KEY", "x")
    with pytest.raises(EnvGuardRefused):
        guard(_settings("demo"))


def test_live_config_uses_the_live_tag(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    assert build_config(_settings("live")).trader_id.value == "TRADER-LIVE"


def test_explicit_trader_id_with_matching_tag_is_used(monkeypatch):
    monkeypatch.setenv("TRADER_ID", "TRADER-demo")
    assert build_config(_settings("demo")).trader_id.value == "TRADER-demo"


def test_cache_points_at_the_configured_redis(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    settings = Settings(trading_env="demo", redis_host="cache.internal", redis_port=6380)

    database = build_config(settings).cache.database

    assert (database.host, database.port) == ("cache.internal", 6380)


def test_trader_in_live_may_see_kraken_keys(monkeypatch):
    # covers: Env guard; live execution (features 4 and 6) will need them.
    from trader.main import guard

    monkeypatch.delenv("TRADER_ID", raising=False)
    monkeypatch.setenv("KRAKEN_FUTURES_API_KEY", "x")
    guard(_settings("live"))


class _FakeNode:
    instances: list = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        _FakeNode.instances.append(self)

    @property
    def trader(self):
        return self

    def add_actor(self, actor):
        self.calls.append(("add_actor", type(actor).__name__))

    def add_data_client_factory(self, name, factory):
        self.calls.append(("factory", name))

    def build(self):
        self.calls.append("build")

    def run(self):
        self.calls.append("run")
        if getattr(self, "fail_run", False):
            raise RuntimeError("engine crashed")

    def dispose(self):
        self.calls.append("dispose")


@pytest.fixture
def fake_main(monkeypatch):
    import trader.main as trader_main

    _FakeNode.instances = []
    monkeypatch.delenv("TRADER_ID", raising=False)
    monkeypatch.setattr(trader_main, "boot", lambda kind, guard: _settings("demo"))
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
