import logging
import os

import pytest

from trading_core.secrets import reset_registry

# The dev container loads .env, so these are set before any test runs.
_ENV_PREFIXES = (
    "TRADING_ENV",
    "PROCESS_NAME",
    "LOG_LEVEL",
    "CLOCK_SERVERS",
    "MAX_CLOCK_OFFSET_MS",
    "POSTGRES_",
    "REDIS_",
    "KRAKEN_",
    "GIT_COMMIT",
    "SECRETS_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with none of our settings in it."""
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def clean_secret_registry():
    """Loaded secrets are registered process wide for redaction; forget them between tests."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def secret_files(tmp_path, monkeypatch):
    """Write secret files into a temporary folder that Settings points at; returns a writer."""
    monkeypatch.setenv("SECRETS_DIR_IN_CONTAINER", str(tmp_path))

    def write(**values: str):
        for name, value in values.items():
            (tmp_path / name).write_text(value)
        return tmp_path

    return write


@pytest.fixture(autouse=True)
def restore_logging():
    """configure_logging replaces the root handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
