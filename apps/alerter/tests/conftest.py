import os
from datetime import UTC, datetime, timedelta

import pytest

from alerter.delivery import Outcome
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

    def _check(self) -> None:
        if self.down:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("connect", {}, Exception("postgres is down"))

    def save(self, episode: Episode) -> int:
        self._check()
        self.saves.append(episode)
        self.rows[(episode.fingerprint, episode.opened_at)] = episode
        return len(self.rows)

    def load_active(self, failed_since: datetime) -> list[Episode]:
        self._check()
        return [
            e
            for e in self.rows.values()
            if e.is_open or (e.delivery is Delivery.FAILED and e.opened_at >= failed_since)
        ]


class FakeRedis:
    """Holds string keys; ``fail`` makes every call raise like a dead Redis."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail = False

    async def get(self, name: str) -> str | None:
        if self.fail:
            from redis.exceptions import ConnectionError

            raise ConnectionError("redis is down")
        return self.values.get(name)
