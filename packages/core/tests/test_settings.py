"""covers: spec 0001 configuration, spec 0002 git_commit, spec 0003 secrets folder and database URL."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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
    settings = Settings(trading_env="demo", postgres_user="u", postgres_host="h", postgres_port=5433, postgres_db="d")

    assert settings.database_url(SecretStr("p")) == "postgresql+psycopg://u:p@h:5433/d"
    assert settings.database_url(SecretStr("p"), "other").endswith("/other")


def test_database_url_quotes_user_and_password():
    settings = Settings(trading_env="demo", postgres_user="a@b")

    assert settings.database_url(SecretStr("p:w/d@#")).startswith("postgresql+psycopg://a%40b:p%3Aw%2Fd%40%23@")


def test_no_password_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env")

    assert "postgres_password" not in Settings().model_dump()


def test_secrets_dir_defaults_to_run_secrets_and_only_the_test_alias_moves_it(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("SECRETS_DIR", "/host/only")  # the Compose substitution variable is ignored

    assert Settings().secrets_dir == Path("/run/secrets")

    monkeypatch.setenv("SECRETS_DIR_IN_CONTAINER", str(tmp_path))
    assert Settings().secrets_dir == tmp_path


def test_redis_username_defaults_to_none_and_reads_the_environment(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    assert Settings().redis_username is None

    monkeypatch.setenv("REDIS_USERNAME", "dashboard")
    assert Settings().redis_username == "dashboard"


def test_git_commit_defaults_to_unknown_and_reads_the_environment(monkeypatch):
    assert Settings(trading_env="demo").git_commit == "unknown"

    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("GIT_COMMIT", "abc123")

    assert Settings().git_commit == "abc123"
