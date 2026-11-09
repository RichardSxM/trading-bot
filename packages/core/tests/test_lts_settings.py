"""covers: spec 0031 AC-2 (the live lock reads as False unless set, and only the lock's two homes read it)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_core.lts_settings import LtsSettings

REPO = Path(__file__).resolve().parents[3]
ALLOWED = {"packages/core/src/trading_core/lts_settings.py", "apps/lab/src/lab/main.py"}


@pytest.fixture(autouse=True)
def no_lock_in_env(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)


def test_omitted_is_false():
    assert LtsSettings().live_trading_enabled is False


def test_false_is_false(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")

    assert LtsSettings().live_trading_enabled is False


def test_true_reads_true_so_the_command_can_refuse(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    assert LtsSettings().live_trading_enabled is True


def test_a_value_that_is_not_a_bool_is_refused(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "maybe")

    with pytest.raises(ValidationError):
        LtsSettings()


def test_the_feed_limits_have_the_specs_defaults():
    settings = LtsSettings()

    assert settings.lts_book_max_age_ms == 500
    assert settings.lts_max_processing_lag_ms == 250
    assert settings.lts_trade_silence_s == 30
    assert settings.lts_instrument_meta_max_age_s == 3600


def test_nothing_else_reads_the_lock():
    readers = {
        path.relative_to(REPO).as_posix()
        for folder in ("packages", "apps")
        for path in (REPO / folder).rglob("src/**/*.py")
        if "live_trading_enabled" in path.read_text(encoding="utf-8").lower()
    }

    assert readers <= ALLOWED
    assert "packages/core/src/trading_core/lts_settings.py" in readers
