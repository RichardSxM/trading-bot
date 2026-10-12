"""covers: spec 0001 configuration, spec 0002 git_commit, spec 0003 secrets folder and database URL."""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from trading_core.settings import CatalogDiskSettings, ContextSettings, Settings


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


# spec 0006: the paper loop settings, validated at start.


def test_paper_loop_defaults_match_the_spec():
    from decimal import Decimal

    from trading_core.settings import PaperLoopSettings

    s = PaperLoopSettings(trading_env="demo")

    assert (s.placeholder_interval_min, s.placeholder_hold_min, s.placeholder_lookback_min) == (15, 10, 5)
    assert s.placeholder_stop_pct == Decimal("0.3")
    assert (s.placeholder_notional_usd, s.risk_max_order_notional_usd, s.risk_max_leverage) == (
        Decimal(100),
        Decimal(250),
        Decimal(2),
    )
    assert (s.sandbox_maker_fee, s.sandbox_taker_fee) == (Decimal("0.0002"), Decimal("0.0005"))
    assert (s.recording_queue_max, s.spool_dir) == (10_000, Path("/data/spool"))


def test_paper_loop_reads_decimals_exactly_from_the_environment(monkeypatch):
    from decimal import Decimal

    from trading_core.settings import PaperLoopSettings

    monkeypatch.setenv("PLACEHOLDER_STOP_PCT", "0.1")
    monkeypatch.setenv("SANDBOX_TAKER_FEE", "0.00045")

    s = PaperLoopSettings(trading_env="demo")

    assert (s.placeholder_stop_pct, s.sandbox_taker_fee) == (Decimal("0.1"), Decimal("0.00045"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLACEHOLDER_INTERVAL_MIN", "7"),
        ("PLACEHOLDER_INTERVAL_MIN", "0"),
        ("PLACEHOLDER_HOLD_MIN", "15"),
        ("PLACEHOLDER_HOLD_MIN", "0"),
        ("PLACEHOLDER_LOOKBACK_MIN", "0"),
        ("PLACEHOLDER_STOP_PCT", "0"),
        ("PLACEHOLDER_STOP_PCT", "100"),
        ("PLACEHOLDER_NOTIONAL_USD", "0"),
        ("RISK_MAX_ORDER_NOTIONAL_USD", "-1"),
        ("RISK_MAX_LEVERAGE", "0"),
        ("RISK_STALE_QUOTE_S", "0"),
        ("STOP_ACCEPT_TIMEOUT_S", "0"),
        ("SANDBOX_STARTING_BALANCE_USD", "0"),
        ("SANDBOX_MAKER_FEE", "-0.1"),
        ("SANDBOX_TAKER_FEE", "1"),
        ("RECORDING_MAX_BACKLOG_S", "0"),
        ("RECORDING_QUEUE_MAX", "0"),
        ("RECORDING_DRAIN_S", "0"),
    ],
)
def test_paper_loop_refuses_a_bad_value(monkeypatch, name, value):
    from trading_core.settings import PaperLoopSettings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        PaperLoopSettings(trading_env="demo")


def test_the_stop_reject_drill_is_off_by_default_and_allowed_in_demo(monkeypatch):
    from trading_core.settings import PaperLoopSettings

    assert PaperLoopSettings(trading_env="demo").sandbox_reject_stop_orders is False

    monkeypatch.setenv("SANDBOX_REJECT_STOP_ORDERS", "true")
    assert PaperLoopSettings(trading_env="demo").sandbox_reject_stop_orders is True


def test_the_kill_switch_drill_is_off_by_default_and_refused_outside_demo(monkeypatch):
    """covers: spec 0010 SANDBOX_REJECT_KILL_SWITCH_ORDERS"""
    from trading_core.settings import PaperLoopSettings

    assert PaperLoopSettings(trading_env="demo").sandbox_reject_kill_switch_orders is False
    monkeypatch.setenv("SANDBOX_REJECT_KILL_SWITCH_ORDERS", "true")
    assert PaperLoopSettings(trading_env="demo").sandbox_reject_kill_switch_orders is True
    with pytest.raises(ValidationError, match="SANDBOX_REJECT_KILL_SWITCH_ORDERS"):
        PaperLoopSettings(trading_env="live")


def test_the_stop_reject_drill_is_refused_outside_demo(monkeypatch):
    from trading_core.settings import PaperLoopSettings

    monkeypatch.setenv("SANDBOX_REJECT_STOP_ORDERS", "true")
    with pytest.raises(ValidationError, match="SANDBOX_REJECT_STOP_ORDERS"):
        PaperLoopSettings(trading_env="live")


def test_strategy_config_holds_the_placeholder_and_risk_settings_only():
    from trading_core.settings import PaperLoopSettings

    config = PaperLoopSettings(trading_env="demo").strategy_config()

    assert config["placeholder_interval_min"] == 15
    assert config["placeholder_stop_pct"] == "0.3"
    assert config["risk_max_order_notional_usd"] == "250"
    assert all(name.startswith(("placeholder_", "risk_")) for name in config)
    assert "sandbox_taker_fee" not in config


def test_catalog_floors_default_to_the_spec(monkeypatch):
    """covers: spec 0007 AC-6, AC-7."""
    monkeypatch.setenv("TRADING_ENV", "demo")

    settings = CatalogDiskSettings()

    assert (settings.catalog_free_floor_gb, settings.catalog_hard_floor_gb) == (Decimal(8), Decimal(2))


@pytest.mark.parametrize("hard", ["8", "9"])
def test_hard_floor_must_be_below_the_free_floor(monkeypatch, hard):
    """covers: spec 0007 AC-7 (each process refuses to start on a bad pair)."""
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("CATALOG_FREE_FLOOR_GB", "8")
    monkeypatch.setenv("CATALOG_HARD_FLOOR_GB", hard)

    with pytest.raises(ValidationError, match="CATALOG_HARD_FLOOR_GB"):
        CatalogDiskSettings()


def test_context_settings_default_to_the_spec(monkeypatch):
    """covers: spec 0009 Configuration, AC-9."""
    monkeypatch.setenv("TRADING_ENV", "demo")

    settings = ContextSettings()

    assert settings.context_ws_url == "wss://futures.kraken.com/ws/v1"
    assert (settings.context_reconnect_max_s, settings.context_idle_s, settings.context_silence_s) == (60, 30, 60)
    assert (
        settings.context_max_age_mark_s,
        settings.context_max_age_index_s,
        settings.context_max_age_open_interest_s,
        settings.context_max_age_funding_s,
        settings.context_liquidation_window_s,
    ) == (30, 30, 30, 7_200, 60)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CONTEXT_WS_URL", "ws://futures.kraken.com/ws/v1"),
        ("CONTEXT_WS_URL", "https://futures.kraken.com"),
        ("CONTEXT_RECONNECT_MAX_S", "0"),
        ("CONTEXT_IDLE_S", "4"),
        ("CONTEXT_LIQUIDATION_WINDOW_S", "0"),
        ("CONTEXT_MAX_AGE_MARK_S", "0"),
    ],
)
def test_context_settings_refuse_a_bad_value(monkeypatch, name, value):
    """covers: spec 0009 AC-16 (only wss://), Configuration bounds."""
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name if name == "CONTEXT_WS_URL" else name.lower()):
        ContextSettings()
