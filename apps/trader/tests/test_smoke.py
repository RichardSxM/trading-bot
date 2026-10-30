import asyncio
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar, cast

import pytest
from pydantic import SecretStr

from trader.main import TraderSettings, build_config
from trading_core.boot import Boot
from trading_core.naming import NodeIdMismatch
from trading_core.reconcile.views import ExchangeUnavailable
from trading_core.recording.errors import PromotionRequiredError
from trading_core.settings import TradingEnv

SECRETS = {
    "redis_password": SecretStr("r" * 32),
    "postgres_password": SecretStr("p" * 32),
    "kraken_api_key": SecretStr("k" * 32),
    "kraken_api_secret": SecretStr("s" * 32),
}


class _UnreachableExchange:
    """Kraken's view for a live run_node: never reached in these tests (spec 0023)."""

    async def fetch(self):
        raise ExchangeUnavailable("not in tests")

    async def cancel(self, order):
        raise ExchangeUnavailable("not in tests")


EXCHANGE = _UnreachableExchange()


def _settings(env: TradingEnv, live_mode: str | None = None) -> TraderSettings:
    # A live node runs only in drill mode (spec 0023 AC-22), so live tests default to it.
    if live_mode is None:
        live_mode = "drill" if env == "live" else "off"
    return TraderSettings(
        trading_env=env,
        redis_host="redis",
        redis_port=6379,
        kraken_key_rotated_on=date(2026, 9, 1),
        live_mode=live_mode,
    )


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
    # Demo orders go only to the sandbox (spec 0006 AC-2).
    assert list(config.exec_clients) == ["SANDBOX"]


def test_explicit_trader_id_must_match_env(monkeypatch):
    monkeypatch.setenv("TRADER_ID", "TRADER-LIVE")
    with pytest.raises(NodeIdMismatch):
        build_config(_settings("demo"), SECRETS)


def test_data_client_reads_live_public_feeds_in_demo(monkeypatch):
    from nautilus_trader.adapters.kraken import KrakenEnvironment

    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"), SECRETS)
    assert config.data_clients["KRAKEN"].environment == KrakenEnvironment.LIVE


def test_config_guard_passes_a_demo_node_with_only_the_sandbox(monkeypatch):
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
    settings = TraderSettings(trading_env="demo", redis_host="cache.internal", redis_port=6380)

    cache = build_config(settings, SECRETS).cache
    assert cache is not None
    database = cache.database
    assert database is not None

    assert (database.host, database.port) == ("cache.internal", 6380)


def test_cache_logs_in_as_the_services_redis_user(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    settings = TraderSettings(trading_env="demo", redis_username="trader")

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

    def add_strategy(self, strategy):
        self.strategies_added = [*getattr(self, "strategies_added", []), strategy]
        self.calls.append(("add_strategy", type(strategy).__name__))

    def strategies(self):
        return list(getattr(self, "strategies_added", []))

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

    # What the startup watchdog reads (spec 0023 AC-5).
    is_running = True

    # What the heartbeat snapshot reads (spec 0004 AC-8).
    class state:
        name = "RUNNING"

    class kernel:
        class data_engine:
            @staticmethod
            def check_connected():
                return True

        exec_engine = None  # live hands it to the reconciliation's report sink (spec 0023)


class _FakeRedis:
    """Records snapshot writes instead of reaching Redis."""

    def __init__(self):
        self.writes = []

    async def set(self, name, value, *, ex):
        self.writes.append((name, value, ex))


class ControlActor:
    """Stands in for the control actor, which needs a real node and Redis."""


class KillSwitch:
    """Stands in for the kill switch; records the strategies handed over."""

    def attach(self, strategies, record, run_id):
        self.attached = (strategies, record, run_id)

    def watch_losses(self, losses):
        self.losses = losses

    def watch_exchange(self, exchange_flat):
        self.exchange_flat = exchange_flat


def _fake_control_parts(node, settings, secrets, recheck=None, drill=None):
    from trader.main import ControlParts

    node.kill_switch = KillSwitch()
    return ControlParts(
        actor=cast(Any, ControlActor()),
        kill_switch=cast(Any, node.kill_switch),
        risk_engine=cast(Any, object()),
        shutdown_counts=lambda: {"cancelled": 0, "unacknowledged_cancels": 0, "positions_kept": 0, "stops_kept": 0},
        untrusted=cast(Any, object()),
    )


@pytest.fixture
def fake_main(monkeypatch):
    import trader.main as trader_main

    _FakeNode.instances = []
    monkeypatch.delenv("TRADER_ID", raising=False)
    monkeypatch.setattr(trader_main, "boot", lambda kind, **kw: Boot(_settings("demo"), SECRETS))
    monkeypatch.setattr(trader_main, "TradingNode", _FakeNode)
    monkeypatch.setattr(trader_main, "snapshot_client", lambda settings, password: _FakeRedis())
    monkeypatch.setattr(trader_main, "control_parts", _fake_control_parts)
    monkeypatch.setattr(
        trader_main, "trader_state", lambda gate, risk, cache, commit, paper=None, *_: {"paused": False}
    )
    monkeypatch.setattr(trader_main, "cache_client", lambda settings, password: _PurgeRedis())
    monkeypatch.setattr(trader_main, "start_paper_loop", lambda settings, secrets, trader_id: None)
    return trader_main


def test_main_refuses_to_start_when_redis_is_unreachable(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: False)

    with pytest.raises(SystemExit) as exit_info:
        fake_main.main()

    assert exit_info.value.code == 1
    assert _FakeNode.instances == []


def test_main_exits_78_without_a_paper_promotion(fake_main, monkeypatch, caplog):
    """covers: spec 0015 AC-14 (paper_promotion_required at critical, exit 78, no node built)."""

    def refused(settings, secrets, trader_id):
        raise PromotionRequiredError("latest candidate → paper promotion is missing")

    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)
    monkeypatch.setattr(fake_main, "start_paper_loop", refused)

    with caplog.at_level(logging.CRITICAL, logger="trader"), pytest.raises(SystemExit) as exit_info:
        fake_main.main()

    assert exit_info.value.code == 78
    critical = [getattr(r, "fields", {}).get("code") for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical == ["paper_promotion_required"]
    assert _FakeNode.instances == []


def test_main_builds_and_runs_the_node_with_a_heartbeat(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)

    fake_main.main()

    (node,) = _FakeNode.instances
    assert node.config.trader_id.value == "TRADER-DEMO"
    assert ("add_actor", "HeartbeatActor") in node.calls
    # The control actor comes first, so a pause is in place before any strategy runs (spec 0005 AC-6).
    assert next(c for c in node.calls if c[0] == "add_actor") == ("add_actor", "ControlActor")
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
        guard(_settings("live", "off"), SECRETS)
    allowlist = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}
    guard(_settings("live", "off"), SECRETS, allowlist)


def test_live_mode_drill_lets_the_live_trader_build_the_kraken_client(monkeypatch):
    # covers: spec 0023 AC-22 (LIVE_MODE=off, the default, keeps live refused exactly as before).
    from trader.main import guard

    monkeypatch.delenv("TRADER_ID", raising=False)
    guard(_settings("live", "drill"), SECRETS)
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("KRAKEN_KEY_ROTATED_ON", "2026-09-01")
    assert TraderSettings().live_mode == "off"


def test_a_live_node_outside_drill_mode_is_refused(fake_main):
    with pytest.raises(ValueError, match="LIVE_MODE=drill"):
        fake_main.run_node(
            build_config(_settings("live", "off"), SECRETS), _settings("live", "off"), 5.0, SECRETS, exchange=EXCHANGE
        )


def test_drill_mode_loads_reconcile_drill_as_the_only_strategy_beside_the_executor(fake_main):
    fake_main.run_node(build_config(_settings("live"), SECRETS), _settings("live"), 5.0, SECRETS, exchange=EXCHANGE)

    (node,) = _FakeNode.instances
    strategies = [name for call, name in (c for c in node.calls if isinstance(c, tuple)) if call == "add_strategy"]
    assert strategies == ["ReconcileDrill", "ReconcileExecutor"]


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
        code = fake_main.run_node(
            build_config(_settings("live"), SECRETS),
            _settings("live"),
            0.1,
            SECRETS,
            fail_backoff_s=0,
            exchange=EXCHANGE,
        )
    finally:
        fake_main.TradingNode = original

    (node,) = _FakeNode.instances
    assert code == 1
    assert "stop" in node.calls
    assert ("add_actor", "KeyAgeActor") in node.calls
    assert ("exec_factory", "KRAKEN") in node.calls
    # Live only: the executor that sends the check's orders (spec 0023 AC-7).
    assert ("add_strategy", "ReconcileExecutor") in node.calls
    assert node.calls[-1] == "dispose"


def test_demo_run_has_no_key_actors_and_exits_0(fake_main):
    code = fake_main.run_node(build_config(_settings("demo"), SECRETS), _settings("demo"), 2.0, SECRETS)

    (node,) = _FakeNode.instances
    assert code == 0
    assert ("add_actor", "KeyAgeActor") not in node.calls
    assert ("exec_factory", "KRAKEN") not in node.calls
    assert ("add_strategy", "ReconcileExecutor") not in node.calls  # demo only reports (spec 0023 AC-23)


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
        code = fake_main.run_node(
            build_config(_settings("live"), SECRETS),
            _settings("live"),
            0.1,
            SECRETS,
            fail_backoff_s=0,
            exchange=EXCHANGE,
        )
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
        code = fake_main.run_node(
            build_config(_settings("live"), SECRETS),
            _settings("live"),
            5.0,
            SECRETS,
            fail_backoff_s=0,
            exchange=EXCHANGE,
        )
    finally:
        fake_main.TradingNode = original

    assert code == 1
    assert "stop" in _FakeNode.instances[0].calls


# spec 0004: the heartbeat snapshot for the dashboard (AC-8)


@pytest.mark.parametrize("env", ["demo", "live"])
def test_every_env_writes_the_heartbeat_snapshot_while_the_node_runs(fake_main, monkeypatch, env):
    import json

    redis = _FakeRedis()
    monkeypatch.setattr(fake_main, "snapshot_client", lambda settings, password: redis)

    class RunningNode(_FakeNode):
        class cache:
            @staticmethod
            def account_for_venue(venue):
                return None

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    monkeypatch.setattr(fake_main, "TradingNode", RunningNode)
    fake_main.run_node(
        build_config(_settings(env), SECRETS), _settings(env), 5.0, SECRETS, fail_backoff_s=0, exchange=EXCHANGE
    )

    key, value, ex = next(w for w in redis.writes if w[0].endswith(":heartbeat"))
    assert (key, ex) == (f"{env}:snapshot:heartbeat", 30)
    assert any(w[0] == f"{env}:snapshot:trader_state" and w[2] == 30 for w in redis.writes)
    assert json.loads(value)["data"] == {
        "process": "trader",
        "git_commit": "unknown",
        "trading_state": "RUNNING",
        "data_connected": True,
    }


# --- gaps found against spec 0004 (/test pass)


def test_a_crashed_heartbeat_writer_logs_critical_and_the_node_keeps_running(fake_main, monkeypatch, caplog):
    """covers: AC-8 (one CRITICAL snapshot_writer_crashed, trading goes on)"""
    import logging

    class Node(_FakeNode):
        class kernel:
            class data_engine:
                @staticmethod
                def check_connected():
                    raise RuntimeError("bug in the state read")

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.05))
            self.calls.append("still running")

    monkeypatch.setattr(fake_main, "TradingNode", Node)
    with caplog.at_level(logging.CRITICAL):
        code = fake_main.run_node(
            build_config(_settings("demo"), SECRETS), _settings("demo"), 5.0, SECRETS, fail_backoff_s=0
        )

    assert code == 0
    assert _FakeNode.instances[0].calls[-3:] == ["run", "still running", "dispose"]
    crashed = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "snapshot_writer_crashed"]
    assert len(crashed) == 1


def test_a_disconnected_data_client_reads_as_false(fake_main, monkeypatch):
    """covers: AC-8 (data_connected is a real bool)"""
    import json

    redis = _FakeRedis()
    monkeypatch.setattr(fake_main, "snapshot_client", lambda settings, password: redis)

    class Node(_FakeNode):
        class kernel:
            class data_engine:
                @staticmethod
                def check_connected():
                    return 0

        def run(self):
            self.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    monkeypatch.setattr(fake_main, "TradingNode", Node)
    fake_main.run_node(build_config(_settings("demo"), SECRETS), _settings("demo"), 5.0, SECRETS, fail_backoff_s=0)
    heartbeat = next(w for w in redis.writes if w[0].endswith(":heartbeat"))
    assert json.loads(heartbeat[1])["data"]["data_connected"] is False


# spec 0005: the control side and the graceful stop (AC-6, AC-7, AC-9)


def test_a_stop_gives_entry_cancels_ten_seconds_to_be_acknowledged(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    assert build_config(_settings("demo"), SECRETS).timeout_post_stop == 10.0


def test_sigterm_stops_the_node_and_a_clean_stop_logs_shutdown_graceful(fake_main, monkeypatch, caplog):
    import logging
    import os
    import signal

    class Node(_FakeNode):
        def run(self):
            self.calls.append("run")
            loop = self.get_event_loop()
            loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
            loop.run_until_complete(asyncio.sleep(0.2))

    monkeypatch.setattr(fake_main, "TradingNode", Node)
    with caplog.at_level(logging.INFO):
        code = fake_main.run_node(
            build_config(_settings("demo"), SECRETS), _settings("demo"), 5.0, SECRETS, fail_backoff_s=0
        )

    assert code == 0
    assert "stop" in _FakeNode.instances[0].calls
    [line] = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "shutdown_graceful"]
    assert line.fields == {
        "code": "shutdown_graceful",
        "cancelled": 0,
        "unacknowledged_cancels": 0,
        "positions_kept": 0,
        "stops_kept": 0,
    }


def test_both_snapshots_are_written_with_the_trader_state_from_the_control_side(fake_main, monkeypatch):
    import json

    redis = _FakeRedis()
    monkeypatch.setattr(fake_main, "snapshot_client", lambda settings, password: redis)

    class Node(_FakeNode):
        def run(self):
            self.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    monkeypatch.setattr(fake_main, "TradingNode", Node)
    fake_main.run_node(build_config(_settings("demo"), SECRETS), _settings("demo"), 5.0, SECRETS, fail_backoff_s=0)

    state = next(w for w in redis.writes if w[0] == "demo:snapshot:trader_state")
    assert state[2] == 30
    assert json.loads(state[1])["data"] == {"paused": False}


# spec 0006: the sandbox, the risk cap, the stop budget, and the demo cache purge.


def test_demo_config_registers_the_fee_sandbox_and_the_risk_engine_cap(monkeypatch):
    from trading_core.node.sandbox import FeeSandboxConfig

    monkeypatch.delenv("TRADER_ID", raising=False)
    config = build_config(_settings("demo"), SECRETS)

    assert isinstance(config.exec_clients["SANDBOX"], FeeSandboxConfig)
    assert config.risk_engine is not None
    assert {k: str(v) for k, v in config.risk_engine.max_notional_per_order.items()} == {"PF_XBTUSD.KRAKEN": "250"}


def test_live_config_never_builds_the_sandbox(monkeypatch):
    monkeypatch.delenv("TRADER_ID", raising=False)
    assert "SANDBOX" not in build_config(_settings("live"), SECRETS).exec_clients


def test_a_live_trader_with_a_sandbox_is_refused(monkeypatch):
    from trader.main import guard
    from trading_core.env_guard import ALLOWED_EXEC_CLIENTS, EnvGuardRefused

    monkeypatch.delenv("TRADER_ID", raising=False)
    monkeypatch.setattr("trader.main.build_config", lambda settings, secrets: _sandbox_config())
    allowlist = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}
    with pytest.raises(EnvGuardRefused, match="SANDBOX"):
        guard(_settings("live"), SECRETS, allowlist)


def _sandbox_config():
    from types import SimpleNamespace

    return SimpleNamespace(exec_clients={"SANDBOX": object()})


def test_the_stop_and_the_recording_drain_must_fit_the_stop_grace():
    from pydantic import ValidationError

    TraderSettings(trading_env="demo", recording_drain_s=Decimal(15))
    with pytest.raises(ValidationError, match="RECORDING_DRAIN_S"):
        TraderSettings(trading_env="demo", recording_drain_s=Decimal(16))


class _PurgeRedis:
    """Holds keys and answers SCAN (with a glob match) and UNLINK, like the Compose Redis would."""

    def __init__(self, keys=()):
        self.keys = set(keys)
        self.unlinks = []

    def scan_iter(self, match, count):
        import fnmatch

        return iter(sorted(k for k in self.keys if fnmatch.fnmatchcase(k, match)))

    def unlink(self, *keys):
        self.unlinks.append(keys)
        found = self.keys & set(keys)
        self.keys -= found
        return len(found)


def test_purge_deletes_only_this_traders_cache_keys():
    from trader.main import purge_demo_cache

    redis = _PurgeRedis(
        {
            "trader-TRADER-DEMO:orders:O-1",
            "trader-TRADER-DEMO:positions:P-1",
            "trader-TRADER-DEMO-2:orders:O-1",
            "demo:trader:pause",
            "demo:control",
            "demo:snapshot:heartbeat",
        }
    )

    assert purge_demo_cache(redis, "TRADER-DEMO") == 2
    assert redis.keys == {
        "trader-TRADER-DEMO-2:orders:O-1",
        "demo:trader:pause",
        "demo:control",
        "demo:snapshot:heartbeat",
    }


def test_purge_unlinks_in_batches(monkeypatch):
    import trader.main as trader_main

    monkeypatch.setattr(trader_main, "PURGE_BATCH", 2)
    redis = _PurgeRedis({f"trader-TRADER-DEMO:k{i}" for i in range(5)})

    assert trader_main.purge_demo_cache(redis, "TRADER-DEMO") == 5
    assert [len(batch) for batch in redis.unlinks] == [2, 2, 1]


def test_main_purges_the_demo_cache_before_the_node_exists(fake_main, monkeypatch):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)
    purged = []

    def purge(client, trader_id):
        assert _FakeNode.instances == []
        purged.append(trader_id)
        return 0

    monkeypatch.setattr(fake_main, "purge_demo_cache", purge)

    fake_main.main()

    assert purged == ["TRADER-DEMO"]
    assert ("exec_factory", "SANDBOX") in _FakeNode.instances[0].calls


@pytest.mark.parametrize("drill", [True, False])
def test_main_warns_only_while_the_stop_reject_drill_is_on(fake_main, monkeypatch, caplog, drill):
    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)
    settings = _settings("demo").model_copy(update={"sandbox_reject_stop_orders": drill})
    monkeypatch.setattr(fake_main, "boot", lambda kind, **kw: Boot(settings, SECRETS))

    fake_main.main()

    warned = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "sandbox_reject_stop_orders"]
    assert [r.levelname for r in warned] == (["WARNING"] if drill else [])


# spec 0006: the paper loop wiring (AC-2, AC-13, AC-15, AC-16)


class _FakeWriter:
    def __init__(self, drained=True):
        self.drained = drained
        self.items = []
        self.spilled = []

    def put(self, item):
        self.items.append(item)

    def drain(self, timeout_s):
        self.timeout_s = timeout_s
        return self.drained

    def spill(self, path):
        self.spilled.append(path)
        return 3


def _paper(writer=None):
    import uuid

    from trader.main import PaperLoop
    from trading_core.node.funding import FundingLedger
    from trading_core.strategy.blocks import EntryBlocks

    return PaperLoop(
        run_id=uuid.uuid7(),
        blocks=EntryBlocks(),
        writer=cast(Any, writer or _FakeWriter()),
        sessions=cast(Any, None),
        ledger=FundingLedger(),
    )


class _PaperNode(_FakeNode):
    class cache:
        @staticmethod
        def positions_open(**kwargs):
            return []

    portfolio = None


def test_demo_registers_recording_then_the_strategy_after_control_and_finishes_the_run(fake_main, monkeypatch):
    finished = []
    monkeypatch.setattr(fake_main, "TradingNode", _PaperNode)
    monkeypatch.setattr(fake_main, "finish_paper_loop", lambda paper, settings, positions: finished.append(positions))
    redis = _FakeRedis()
    monkeypatch.setattr(fake_main, "snapshot_client", lambda settings, password: redis)

    code = fake_main.run_node(build_config(_settings("demo"), SECRETS), _settings("demo"), 2.0, SECRETS, paper=_paper())

    (node,) = _FakeNode.instances
    registered = [c for c in node.calls if c[0] in ("add_actor", "add_strategy")]
    assert registered[0] == ("add_actor", "ControlActor")
    assert registered.index(("add_actor", "RecordingActor")) < registered.index(("add_strategy", "PlaceholderTimed"))
    # The loss limits come after the control actor and before the strategy, and resume reads them.
    assert registered.index(("add_actor", "LossLimitActor")) < registered.index(("add_strategy", "PlaceholderTimed"))
    assert type(node.kill_switch.losses).__name__ == "LossLimitActor"
    # The kill switch gets the strategies (spec 0010): the placeholder, with the paper run's recorder.
    strategies, record, run_id = node.kill_switch.attached
    assert [type(s).__name__ for s in strategies] == ["PlaceholderTimed"]
    assert record is not None
    assert run_id is not None
    assert (code, finished) == (0, [0])
    assert node.calls[-1] == "dispose"


def test_main_exits_1_when_the_paper_run_cannot_start(fake_main, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def unreachable(settings, secrets, trader_id):
        raise OperationalError("connect", {}, Exception("postgres down"))

    monkeypatch.setattr(fake_main, "redis_reachable", lambda host, port: True)
    monkeypatch.setattr(fake_main, "start_paper_loop", unreachable)

    with pytest.raises(SystemExit) as exit_info:
        fake_main.main()

    assert exit_info.value.code == 1
    assert _FakeNode.instances == []


def test_an_undrained_queue_is_spilled_under_the_run_id(monkeypatch, tmp_path):
    from trader.main import finish_paper_loop

    writer = _FakeWriter(drained=False)
    paper = _paper(writer)

    class Sessions:
        def __call__(self):
            raise ConnectionError("postgres down")

    paper = replace(paper, sessions=Sessions())
    settings = TraderSettings(trading_env="demo", spool_dir=tmp_path, recording_drain_s=Decimal(7))

    finish_paper_loop(paper, settings, positions_open=0)  # a failed finish is logged, not raised

    assert writer.timeout_s == 7.0
    assert writer.spilled == [tmp_path / f"{paper.run_id}.jsonl"]


def test_run_config_records_balance_fees_and_fill_source():
    from trader.main import run_config

    config = run_config(_settings("demo"))

    assert config["fill_source"] == "quotes"
    assert (config["oms_type"], config["account_type"]) == ("NETTING", "MARGIN")
    assert config["starting_balance_usd"] == Decimal(1000)


def test_the_demo_config_builds_a_real_node_with_the_fee_sandbox(monkeypatch):
    """A real TradingNode build (no connection): catches factory wiring the fake node cannot."""
    import msgspec
    from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
    from nautilus_trader.config import CacheConfig
    from nautilus_trader.live.node import TradingNode

    from trading_core.node.sandbox import FeeSandboxExecClientFactory, FeeSandboxExecutionClient

    monkeypatch.delenv("TRADER_ID", raising=False)
    # Without the Redis cache database, so the build needs no Redis; the factories are the point.
    config = msgspec.structs.replace(build_config(_settings("demo"), SECRETS), cache=CacheConfig())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    node = TradingNode(config=config, loop=loop)
    try:
        node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
        node.add_exec_client_factory("SANDBOX", FeeSandboxExecClientFactory)
        node.build()
        clients = node.kernel.exec_engine._clients.values()
        assert any(isinstance(c, FeeSandboxExecutionClient) for c in clients)
    finally:
        node.dispose()
        asyncio.set_event_loop(None)
        loop.close()


def test_the_placeholder_reads_the_market_context_with_the_trader_settings():
    """covers: spec 0009 AC-11 (the placeholder logs the context, ages from ContextSettings)."""
    from uuid import uuid4

    from trader.main import placeholder_config

    settings = _settings("demo")

    config = placeholder_config(settings, uuid4())

    assert config.use_market_context is True
    assert (
        config.context_max_age_mark_s,
        config.context_max_age_index_s,
        config.context_max_age_open_interest_s,
        config.context_max_age_funding_s,
        config.context_liquidation_window_s,
    ) == (
        settings.context_max_age_mark_s,
        settings.context_max_age_index_s,
        settings.context_max_age_open_interest_s,
        settings.context_max_age_funding_s,
        settings.context_liquidation_window_s,
    )


def test_the_placeholder_guard_sizes_with_the_sandbox_taker_fee():
    """covers: spec 0011 verify step 2 (the cached Kraken instrument carries fee 0)."""
    from uuid import uuid4

    from trader.main import placeholder_config

    settings = _settings("demo")

    assert placeholder_config(settings, uuid4()).taker_fee == settings.sandbox_taker_fee


def test_the_placeholder_refuses_live_without_a_taker_fee_source():
    from uuid import uuid4

    from trader.main import placeholder_config

    with pytest.raises(ValueError, match="live taker fee"):
        placeholder_config(_settings("live"), uuid4())


# spec 0010 AC-21: the loss limit settings refuse a bad start --------------------------------


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"RISK_DAILY_LOSS_LIMIT_USD": "0"}, "greater than 0"),
        ({"RISK_TOTAL_LOSS_LIMIT_USD": "-1"}, "greater than 0"),
        ({"RISK_DAILY_LOSS_LIMIT_USD": "20", "RISK_TOTAL_LOSS_LIMIT_USD": "10"}, "must not be below"),
        ({"RISK_TOTAL_LOSS_SINCE": "2999-01-01"}, "in the future"),
        ({"RISK_TOTAL_LOSS_SINCE": ""}, "risk_total_loss_since"),  # unset in Compose reaches the trader empty
        ({"RISK_MAX_TRADES_PER_DAY": "0"}, "greater than or equal to 1"),
        ({"RISK_LOSS_COOLDOWN_S": "-1"}, "greater than or equal to 0"),
        ({"KILL_SWITCH_FLAT_TIMEOUT_S": "4"}, "greater than or equal to 5"),
        ({"KILL_SWITCH_FLAT_TIMEOUT_S": "121"}, "less than or equal to 120"),
    ],
)
def test_bad_loss_limit_settings_refuse_the_start(monkeypatch, env, match):
    from pydantic import ValidationError

    monkeypatch.setenv("TRADING_ENV", "demo")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=match):
        TraderSettings()


def test_a_missing_total_loss_since_refuses_the_start(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.delenv("RISK_TOTAL_LOSS_SINCE")
    with pytest.raises(ValidationError, match="risk_total_loss_since"):
        TraderSettings()


def test_the_loss_limit_defaults_match_the_spec(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    settings = TraderSettings()

    assert (
        settings.risk_daily_loss_limit_usd,
        settings.risk_total_loss_limit_usd,
        settings.risk_max_trades_per_day,
        settings.risk_loss_cooldown_s,
        settings.kill_switch_flat_timeout_s,
    ) == (Decimal(20), Decimal(100), 50, 600, 20)
    assert "risk_daily_loss_limit_usd" in settings.strategy_config()


# spec 0023: reconciliation ------------------------------------------------------------------


def test_demo_keeps_nautilus_reconciliation_off():
    engine = build_config(_settings("demo"), SECRETS).exec_engine

    assert engine.reconciliation is False


def test_live_reconciles_with_kraken_at_start_and_runs_no_periodic_checks_of_its_own():
    # covers: spec 0023 *Where the views come from*, the LiveExecEngineConfig table.
    config = build_config(_settings("live"), SECRETS)
    engine = config.exec_engine

    assert engine.reconciliation is True
    assert engine.reconciliation_lookback_mins == 1440
    assert engine.open_check_interval_secs is None
    assert engine.position_check_interval_secs is None
    assert engine.generate_missing_orders is True
    assert (engine.inflight_check_interval_ms, engine.inflight_check_threshold_ms) == (2000, 5000)
    assert config.timeout_reconciliation == 30


def test_a_live_node_without_krakens_view_refuses_to_build(fake_main):
    with pytest.raises(ValueError, match="exchange view"):
        fake_main.run_node(build_config(_settings("live"), SECRETS), _settings("live"), 5.0, SECRETS)

    assert _FakeNode.instances == []


@pytest.mark.parametrize("env", ["demo", "live"])
def test_every_env_registers_the_reconcile_actor_before_any_strategy(fake_main, env):
    fake_main.run_node(
        build_config(_settings(env), SECRETS), _settings(env), 5.0, SECRETS, fail_backoff_s=0, exchange=EXCHANGE
    )

    (node,) = _FakeNode.instances
    actors = [name for kind, name in (c for c in node.calls if isinstance(c, tuple)) if kind == "add_actor"]
    assert "ReconcileActor" in actors


def test_a_live_trader_that_never_runs_is_stopped_by_the_watchdog(fake_main, monkeypatch):
    # covers: spec 0023 AC-5, the startup watchdog.
    class NeverRunningNode(_FakeNode):
        is_running = False

        class cache:
            @staticmethod
            def account_for_venue(venue):
                return None

        def run(self):
            self.calls.append("run")
            self.get_event_loop().run_until_complete(asyncio.sleep(0.3))

    monkeypatch.setattr(fake_main, "TradingNode", NeverRunningNode)
    settings = _settings("live").model_copy(update={"reconcile_nautilus_timeout_s": 1})
    monkeypatch.setattr(fake_main, "WATCHDOG_GRACE_S", -1)  # a 0 s watchdog, so it fires inside run()

    code = fake_main.run_node(
        build_config(settings, SECRETS), settings, 60.0, SECRETS, fail_backoff_s=0, exchange=EXCHANGE
    )

    assert code == 1
    assert "stop" in _FakeNode.instances[0].calls
