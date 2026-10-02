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
