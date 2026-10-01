"""covers: spec 0001 configuration, spec 0002 git_commit and role passwords (AC-13)."""

import pytest
from pydantic import ValidationError

from trading_core.settings import RoleSettings, Settings


def test_trading_env_is_required():
    with pytest.raises(ValidationError, match="trading_env"):
        Settings()


@pytest.mark.parametrize("value", ["paper", "DEMO-ish", ""])
def test_trading_env_accepts_only_demo_or_live(monkeypatch, value):
    monkeypatch.setenv("TRADING_ENV", value)
    with pytest.raises(ValidationError):
        Settings()


def test_reads_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("process_name", "recorder")  # names are case insensitive
    monkeypatch.setenv("REDIS_PORT", "6380")

    settings = Settings()

    assert (settings.trading_env, settings.process_name, settings.redis_port) == ("live", "recorder", 6380)


def test_unknown_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("SOMETHING_ELSE", "x")

    Settings()


def test_defaults_match_the_spec():
    settings = Settings(trading_env="demo")

    assert settings.log_level == "INFO"
    assert settings.max_clock_offset_ms == 250
    assert settings.clock_server_list == ["0.pool.ntp.org", "1.pool.ntp.org", "2.pool.ntp.org"]


def test_log_level_is_upper_cased():
    assert Settings(trading_env="demo", log_level="debug").log_level == "DEBUG"


def test_clock_server_list_trims_and_drops_blanks():
    settings = Settings(trading_env="demo", clock_servers=" a.example , ,b.example,")

    assert settings.clock_server_list == ["a.example", "b.example"]


@pytest.mark.parametrize("value", [0, -1])
def test_max_clock_offset_must_be_positive(value):
    with pytest.raises(ValidationError):
        Settings(trading_env="demo", max_clock_offset_ms=value)


def test_database_url_uses_psycopg_and_every_part():
    settings = Settings(
        trading_env="demo",
        postgres_user="u",
        postgres_password="p",
        postgres_host="h",
        postgres_port=5433,
        postgres_db="d",
    )

    assert settings.database_url == "postgresql+psycopg://u:p@h:5433/d"


def test_database_url_quotes_user_and_password():
    settings = Settings(trading_env="demo", postgres_user="a@b", postgres_password="p:w/d@#")

    assert settings.database_url.startswith("postgresql+psycopg://a%40b:p%3Aw%2Fd%40%23@")


def test_git_commit_defaults_to_unknown_and_reads_the_environment(monkeypatch):
    assert Settings(trading_env="demo").git_commit == "unknown"

    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("GIT_COMMIT", "abc123")

    assert Settings().git_commit == "abc123"


def _set_role_passwords(monkeypatch, **overrides):
    values = {
        "POSTGRES_TRADER_PASSWORD": "t",
        "POSTGRES_DASHBOARD_PASSWORD": "d",
        "POSTGRES_BACKUP_PASSWORD": "b",
    } | overrides
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_role_settings_read_every_password(monkeypatch):
    _set_role_passwords(monkeypatch)

    roles = RoleSettings()

    assert (roles.postgres_trader_password, roles.postgres_dashboard_password, roles.postgres_backup_password) == (
        "t",
        "d",
        "b",
    )


@pytest.mark.parametrize("value", [None, ""])
def test_role_settings_refuse_a_missing_or_empty_password(monkeypatch, value):
    _set_role_passwords(monkeypatch, POSTGRES_DASHBOARD_PASSWORD=value)

    with pytest.raises(ValidationError, match="postgres_dashboard_password"):
        RoleSettings()
