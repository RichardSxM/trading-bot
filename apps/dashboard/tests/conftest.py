import os
import time

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr

from dashboard.settings import DashboardSettings
from dashboard.web.app import create_app
from trading_core.snapshot.envelope import encode

PASSWORD = "correct horse battery"
SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # 160 bits, fixed so tests can compute codes
# Cheap argon2id parameters keep the tests fast; verify reads them from the hash.
PASSWORD_HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
SESSION_KEY = "k" * 48

SECRETS = {
    "postgres_password": SecretStr("p" * 32),
    "redis_password": SecretStr("r" * 32),
    "dashboard_password_hash": SecretStr(PASSWORD_HASH),
    "dashboard_totp_seed": SecretStr(SEED),
    "dashboard_session_key": SecretStr(SESSION_KEY),
}

_ENV_PREFIXES = ("TRADING_ENV", "DASHBOARD_", "CONTROL_", "REDIS_", "POSTGRES_", "SECRETS_DIR", "GIT_COMMIT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from an environment with none of our settings in it."""
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name)


class FakeRedis:
    """Holds keys in a dict; ``fail`` makes every call raise like a dead Redis."""

    def __init__(self):
        self.values = {}
        self.fail = None

    async def get(self, name):
        if self.fail is not None:
            raise self.fail
        return self.values.get(name)

    async def aclose(self):
        pass


class Clock:
    """A settable UTC clock in seconds; ``ns`` follows it."""

    def __init__(self, now=None):
        self.now = float(now if now is not None else time.time())

    def __call__(self):
        return self.now

    def ns(self):
        return int(self.now * 1_000_000_000)


def code_at(now, offset_steps=0):
    return pyotp.TOTP(SEED).at(now + offset_steps * 30)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def redis(clock):
    fake = FakeRedis()
    fake.values["demo:snapshot:heartbeat"] = encode(clock.ns(), {"process": "trader"})
    return fake


def make_app(redis, clock, env="demo", **settings):
    settings.setdefault("dashboard_cookie_secure", False)
    return create_app(
        DashboardSettings(trading_env=env, **settings), SECRETS, redis=redis, clock=clock, clock_ns=clock.ns
    )


@pytest.fixture
def client(redis, clock):
    with TestClient(make_app(redis, clock), follow_redirects=False) as test_client:
        yield test_client


def log_in(client, clock, next_path="/"):
    return client.post("/login", data={"password": PASSWORD, "code": code_at(clock.now), "next": next_path})


@pytest.fixture
def logged_in(client, clock):
    assert log_in(client, clock).status_code == 303
    clock.now += 30  # the next login needs a newer step (replay guard)
    return client
