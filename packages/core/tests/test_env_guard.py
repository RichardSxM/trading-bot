"""covers: spec 0001 env guard, spec 0003 AC-4 (Kraken key variables refused everywhere)."""

import pytest

from trading_core.env_guard import (
    ALLOWED_EXEC_CLIENTS,
    KRAKEN_KEY_VARS,
    EnvGuardRefused,
    check_environ,
    check_exec_clients,
)


def test_no_exec_clients_and_no_keys_passes():
    check_environ("trader", environ={})
    check_exec_clients({}, process="trader", env="demo")
    check_exec_clients({}, process="recorder", env="live")


@pytest.mark.parametrize("process", ["trader", "recorder"])
@pytest.mark.parametrize("env", ["demo", "live"])
def test_exec_client_outside_allowlist_is_refused(process, env):
    with pytest.raises(EnvGuardRefused, match="KRAKEN"):
        check_exec_clients({"KRAKEN": object()}, process=process, env=env)


def test_kraken_is_not_allowed_in_live_until_go_live():
    assert ALLOWED_EXEC_CLIENTS[("trader", "live")] == frozenset()


def test_a_passed_allowlist_replaces_the_module_default():
    allowlist = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}

    check_exec_clients({"KRAKEN": object()}, process="trader", env="live", allowlist=allowlist)


@pytest.mark.parametrize("process", ["trader", "recorder", "dashboard", "migrate"])
@pytest.mark.parametrize("var", KRAKEN_KEY_VARS)
def test_any_kraken_key_variable_is_refused_in_every_process(process, var):
    with pytest.raises(EnvGuardRefused, match=var):
        check_environ(process, environ={var: "abc"})


def test_empty_key_var_is_allowed():
    check_environ("trader", environ={"KRAKEN_FUTURES_API_KEY": "  "})


def test_refusal_never_contains_the_secret_value():
    with pytest.raises(EnvGuardRefused) as exc:
        check_environ("trader", environ={"KRAKEN_FUTURES_API_SECRET": "s3cr3t-value"})
    assert "s3cr3t-value" not in str(exc.value)


# spec 0006 AC-2: the sandbox is the demo trader's only execution client.


def test_sandbox_is_allowed_only_for_the_demo_trader():
    from trading_core.env_guard import SANDBOX, NodeProcess
    from trading_core.settings import TradingEnv

    check_exec_clients({SANDBOX: object()}, process="trader", env="demo")
    refused: tuple[tuple[NodeProcess, TradingEnv], ...] = (
        ("trader", "live"),
        ("recorder", "demo"),
        ("recorder", "live"),
    )
    for process, env in refused:
        with pytest.raises(EnvGuardRefused, match=SANDBOX):
            check_exec_clients({SANDBOX: object()}, process=process, env=env)


def test_the_demo_trader_may_not_build_kraken_next_to_the_sandbox():
    from trading_core.env_guard import SANDBOX

    with pytest.raises(EnvGuardRefused, match="KRAKEN"):
        check_exec_clients({SANDBOX: object(), "KRAKEN": object()}, process="trader", env="demo")
