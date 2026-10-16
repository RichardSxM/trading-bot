import os
from datetime import UTC, datetime, timedelta

import pytest

from alerter.delivery import Outcome
from trading_core.alerts.daily import DailyFigures
from trading_core.alerts.model import Delivery, Episode, Push
from trading_core.secrets import reset_registry

_ENV_PREFIXES = ("TRADING_ENV", "ALERTS_", "REDIS_", "POSTGRES_", "SECRETS_DIR", "GIT_COMMIT", "PROCESS_NAME")

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


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


class Clock:
    """A settable UTC clock."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class FakeDeliverer:
    """Records every push and answers with ``delivery``."""

    def __init__(self, delivery: Delivery = Delivery.DRY_RUN) -> None:
        self.delivery = delivery
        self.sent: list[Push] = []

    async def send(self, push: Push) -> Outcome:
        self.sent.append(push)
        return Outcome(self.delivery, "fake")


class FakeStore:
    """An in memory ``EpisodeStore``; ``down`` makes every call raise like an unreachable Postgres."""

    def __init__(self, stored: list[Episode] | None = None) -> None:
        self.rows: dict[tuple[str, datetime], Episode] = {(e.fingerprint, e.opened_at): e for e in stored or []}
        self.saves: list[Episode] = []
        self.down = False
        self.fills: dict[str, dict[str, str]] = {}
        self.fill_reads: list[str] = []
        self.figure_reads: list[datetime] = []

    def _check(self) -> None:
        if self.down:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("connect", {}, Exception("postgres is down"))

    def save(self, episode: Episode) -> int:
        self._check()
        self.saves.append(episode)
        self.rows[(episode.fingerprint, episode.opened_at)] = episode
        return len(self.rows)

    def ping(self) -> None:
        self._check()

    def fill_fields(self, trade_id: str) -> dict[str, str] | None:
        self.fill_reads.append(trade_id)
        self._check()
        return self.fills.get(trade_id)

    def daily_sent(self, fingerprint: str) -> bool:
        self._check()
        return any(e.rule == "daily" and e.fingerprint == fingerprint for e in self.rows.values())

    def daily_figures(self, now: datetime) -> DailyFigures:
        self._check()
        self.figure_reads.append(now)
        return DailyFigures(opened={}, still_open=0, failed=0, failed_titles=(), down_s=0, trades=0)

    def load_active(self, failed_since: datetime) -> list[Episode]:
        self._check()
        return [
            e
            for e in self.rows.values()
            if e.is_open or (e.delivery is Delivery.FAILED and e.opened_at >= failed_since)
        ]


class FakeRedis:
    """Holds string keys (and the TTL each was set with); ``fail`` makes every call raise like a dead Redis."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            from redis.exceptions import ConnectionError

            raise ConnectionError("redis is down")

    async def get(self, name: str) -> str | None:
        self._check()
        return self.values.get(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self._check()
        self.values[name] = value
        self.ttls[name] = ex
        return True

    async def delete(self, *names: str) -> int:
        self._check()
        gone = [n for n in names if n in self.values]
        for name in gone:
            del self.values[name]
            self.ttls.pop(name, None)
        return len(gone)


class FakeStreams(FakeRedis):
    """``FakeRedis`` plus the stream calls of one consumer group per stream (RESP2 shaped replies)."""

    def __init__(self) -> None:
        super().__init__()
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.delivered: dict[str, set[str]] = {}
        self.acked: dict[str, set[str]] = {}

    def append(self, name: str, fields: dict[str, str]) -> str:
        entries = self.streams.setdefault(name, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, dict(fields)))
        return entry_id

    async def xgroup_create(self, name: str, groupname: str, id: str, mkstream: bool) -> bool:
        self._check()
        self.streams.setdefault(name, [])
        return True

    async def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        self._check()
        ((name, start),) = streams.items()
        delivered = self.delivered.setdefault(name, set())
        acked = self.acked.setdefault(name, set())
        if start == "0":
            entries = [(i, f) for i, f in self.streams.get(name, []) if i in delivered and i not in acked]
        else:
            entries = [(i, f) for i, f in self.streams.get(name, []) if i not in delivered]
            delivered.update(i for i, _ in entries)
        entries = entries[:count] if count else entries
        return [[name, entries]] if entries else []

    async def xadd(self, name, fields, id="*", maxlen=None, approximate=True) -> str:
        self._check()
        return self.append(name, {str(k): str(v) for k, v in fields.items()})

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self._check()
        self.acked.setdefault(name, set()).update(ids)
        return len(ids)
