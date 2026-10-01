import logging
import os

import pytest

# The dev container loads .env, so these are set before any test runs.
_ENV_PREFIXES = ("TRADING_ENV", "PROCESS_NAME", "LOG_LEVEL", "CLOCK_SERVERS", "MAX_CLOCK_OFFSET_MS", "POSTGRES_", "REDIS_", "KRAKEN_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with none of our settings in it."""
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def restore_logging():
    """configure_logging replaces the root handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
