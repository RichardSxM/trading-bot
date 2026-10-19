"""Gate test setup: the core tests' ``bundle_factory`` builds the bundles these tests import."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine

from trading_core.secrets import POSTGRES_PASSWORD, read_secret
from trading_core.settings import Settings, get_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"))


@pytest.fixture
def gate_engine(monkeypatch) -> Iterator[Engine]:
    """An engine on the Compose Postgres as ``gate_app`` (the gate service mounts its password)."""
    monkeypatch.setenv("TRADING_ENV", os.environ.get("TRADING_ENV", "demo"))
    get_settings.cache_clear()
    base = Settings(trading_env="demo", postgres_user="gate_app")
    engine = create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=2)
    yield engine
    engine.dispose()
    get_settings.cache_clear()
