import asyncio
import os
import time
from decimal import Decimal

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr

from dashboard.data.pnl import PnlFigures
from dashboard.data.trades import TradesView
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
    """Holds keys and streams in dicts; ``fail`` makes every call raise like a dead Redis.

    ``hang`` makes ``xadd`` wait that many seconds first, like a Redis that does not answer.
    ``reads`` counts the read calls; ``xadd_calls`` records each append's stream and trim arguments.
    """

    def __init__(self):
        self.values = {}
        self.streams = {}
        self.fail = None
        self.hang = 0.0
        self.reads = 0
        self.xadd_calls = []
        self._seq = 0

    async def get(self, name):
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        return self.values.get(name)

    def append(self, name, fields):
        """Append to a stream directly (as the trader would), returning the entry id."""
        self._seq += 1
        entry_id = f"{self._seq}-0".encode()
        encoded = {k.encode(): v.encode() for k, v in fields.items()}
        self.streams.setdefault(name, []).append((entry_id, encoded))
        return entry_id

    async def xadd(self, name, fields, id="*", maxlen=None, approximate=True):
        self.xadd_calls.append({"name": name, "maxlen": maxlen, "approximate": approximate})
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.fail is not None:
            raise self.fail
        entry_id = self.append(name, fields)
        if maxlen is not None:
            self.streams[name] = self.streams[name][-maxlen:]
        return entry_id

    async def xrevrange(self, name, max="+", min="-", count=None):
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        entries = list(reversed(self.streams.get(name, [])))
        return entries if count is None else entries[:count]

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


ZERO_PNL = PnlFigures(realized_today=Decimal(0), realized_all=Decimal(0), fees_today=Decimal(0))


NO_TRADES = TradesView(round_trips=(), fills=())


def make_app(
    redis,
    clock,
    env="demo",
    read_pnl=lambda: ZERO_PNL,
    read_trades=lambda page, fills_page: NO_TRADES,
    read_alerts=lambda: (),
    **settings,
):
    settings.setdefault("dashboard_cookie_secure", False)
    return create_app(
        DashboardSettings(trading_env=env, **settings),
        SECRETS,
        redis=redis,
        clock=clock,
        clock_ns=clock.ns,
        read_pnl=read_pnl,
        read_trades=read_trades,
        read_alerts=read_alerts,
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


def live_client(redis, clock):
    """A logged in client for ``TRADING_ENV=live``: Secure cookies, so it talks https to the test app."""
    redis.values["live:snapshot:heartbeat"] = encode(clock.ns(), {"process": "trader"})
    app = make_app(redis, clock, env="live", dashboard_cookie_secure=True)
    test_client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    return test_client
