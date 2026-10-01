import pytest

from trading_core.env_guard import KRAKEN_KEY_VARS, EnvGuardRefused, check


def test_no_exec_clients_and_no_keys_passes():
    check({}, process="trader", env="demo", environ={})
    check({}, process="recorder", env="live", environ={})


@pytest.mark.parametrize("process", ["trader", "recorder"])
@pytest.mark.parametrize("env", ["demo", "live"])
def test_exec_client_outside_allowlist_is_refused(process, env):
    with pytest.raises(EnvGuardRefused, match="KRAKEN"):
        check({"KRAKEN": object()}, process=process, env=env, environ={})


@pytest.mark.parametrize("var", KRAKEN_KEY_VARS)
def test_any_kraken_key_in_demo_is_refused(var):
    with pytest.raises(EnvGuardRefused, match=var):
        check({}, process="trader", env="demo", environ={var: "abc"})


def test_empty_key_var_is_allowed():
    check({}, process="trader", env="demo", environ={"KRAKEN_FUTURES_API_KEY": "  "})


def test_recorder_is_refused_keys_in_live_too():
    with pytest.raises(EnvGuardRefused):
        check({}, process="recorder", env="live", environ={"KRAKEN_FUTURES_API_KEY": "abc"})


def test_trader_in_live_may_see_keys():
    check({}, process="trader", env="live", environ={"KRAKEN_FUTURES_API_KEY": "abc"})


def test_refusal_never_contains_the_secret_value():
    with pytest.raises(EnvGuardRefused) as exc:
        check({}, process="trader", env="demo", environ={"KRAKEN_FUTURES_API_SECRET": "s3cr3t-value"})
    assert "s3cr3t-value" not in str(exc.value)
