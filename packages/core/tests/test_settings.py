"""covers: spec 0001 configuration (TRADING_ENV required, clock and database settings)."""

import pytest
from pydantic import ValidationError

from trading_core.settings import Settings


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
