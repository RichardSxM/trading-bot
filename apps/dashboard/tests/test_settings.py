"""covers: spec 0004 AC-5, the dashboard refuses bad config and unusable login secrets."""

import pytest
from argon2 import PasswordHasher, Type
from pydantic import SecretStr, ValidationError

from dashboard.settings import CredentialsInvalid, DashboardSettings, credentials_guard

from .conftest import SECRETS


def _with(**overrides):
    return SECRETS | {name: SecretStr(value) for name, value in overrides.items()}


def test_defaults_match_the_spec():
    settings = DashboardSettings(trading_env="demo")
    assert settings.dashboard_cookie_secure is True
    assert (settings.dashboard_stale_amber_s, settings.dashboard_stale_red_s) == (3, 10)
    assert settings.dashboard_poll_ms == 500
    assert (settings.dashboard_session_idle_h, settings.dashboard_session_max_d) == (24, 7)
    assert (settings.control_command_ttl_s, settings.dashboard_command_hide_s) == (300, 900)


def test_insecure_cookies_are_refused_in_live_only(monkeypatch):
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("TRADING_ENV", "live")
    with pytest.raises(ValidationError, match="DASHBOARD_COOKIE_SECURE=false is allowed only in demo"):
        DashboardSettings()
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert DashboardSettings().dashboard_cookie_secure is False


def test_red_must_exceed_amber(monkeypatch):
    monkeypatch.setenv("DASHBOARD_STALE_AMBER_S", "10")
    monkeypatch.setenv("DASHBOARD_STALE_RED_S", "10")
    with pytest.raises(ValidationError, match="must exceed"):
        DashboardSettings(trading_env="demo")


def test_good_credentials_pass_the_guard():
    credentials_guard(DashboardSettings(trading_env="demo"), SECRETS)


def test_a_lowercase_padded_seed_is_accepted():
    credentials_guard(
        DashboardSettings(trading_env="demo"), _with(dashboard_totp_seed="jbswy3dpehpk3pxpjbswy3dpehpk3pxp==")
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"dashboard_password_hash": "plain-text-password-here"}, "not an argon2id hash"),
        (
            {
                "dashboard_password_hash": PasswordHasher(type=Type.I, memory_cost=8, time_cost=1, parallelism=1).hash(
                    "x"
                )
            },
            "argon2id",
        ),
        ({"dashboard_totp_seed": "not base32 at all 1890!"}, "not base32"),
        ({"dashboard_totp_seed": "JBSWY3DPEHPK3PXP"}, "fewer than 160 bits"),
    ],
)
def test_bad_credentials_refuse_with_their_own_code(overrides, match):
    with pytest.raises(CredentialsInvalid, match=match) as info:
        credentials_guard(DashboardSettings(trading_env="demo"), _with(**overrides))
    assert info.value.code == "dashboard_credentials_invalid"


# --- gaps found against spec 0004 (/test pass)


def test_an_argon2d_hash_is_refused():
    """covers: AC-5"""
    argon2d = PasswordHasher(type=Type.D, memory_cost=8, time_cost=1, parallelism=1).hash("x")
    with pytest.raises(CredentialsInvalid, match="argon2id"):
        credentials_guard(DashboardSettings(trading_env="demo"), _with(dashboard_password_hash=argon2d))


def test_a_seed_of_an_impossible_base32_length_is_refused():
    """covers: AC-5"""
    with pytest.raises(CredentialsInvalid, match="not base32"):
        credentials_guard(DashboardSettings(trading_env="demo"), _with(dashboard_totp_seed="A" * 33))


@pytest.mark.parametrize(
    "name",
    [
        "dashboard_stale_amber_s",
        "dashboard_poll_ms",
        "dashboard_session_idle_h",
        "dashboard_session_max_d",
        "control_command_ttl_s",
        "dashboard_command_hide_s",
        "dashboard_ack_warn_s",
    ],
)
def test_a_zero_duration_is_refused(monkeypatch, name):
    """covers: AC-5 (settings validation refuses to start)"""
    monkeypatch.setenv(name.upper(), "0")
    with pytest.raises(ValidationError):
        DashboardSettings(trading_env="demo")


def test_the_guard_refusal_logs_its_own_code_and_message():
    """covers: AC-5 (exit 1 with code dashboard_credentials_invalid)"""
    from trading_core.env_guard import EnvGuardRefused

    assert issubclass(CredentialsInvalid, EnvGuardRefused)
    assert CredentialsInvalid.code == "dashboard_credentials_invalid"
    assert CredentialsInvalid.log_message == "dashboard credentials invalid, refusing to start"
