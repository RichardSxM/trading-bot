"""Builds the alerter from its boot result and runs it (spec 0013)."""

import logging
from collections.abc import Mapping
from typing import Final

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alerter.delivery import Deliverer, LogDelivery, PushoverDelivery
from alerter.pushover import PushoverClient
from alerter.service import Alerter, utc_now
from alerter.settings import AlerterSettings, engine_config
from alerter.store import StoreWriter
from alerter.watchdog import Watchdog
from trading_core.alerts.engine import AlertEngine
from trading_core.boot import Boot
from trading_core.recording.alerts import AlertRepository
from trading_core.secrets import (
    HC_ALERTER_URL,
    POSTGRES_PASSWORD,
    PUSHOVER_APP_TOKEN,
    PUSHOVER_USER_KEY,
    REDIS_PASSWORD,
)

log = logging.getLogger(__name__)

REDIS_TIMEOUT_S: Final = 2.0
DB_CONNECT_TIMEOUT_S: Final = 5


def deliverer(settings: AlerterSettings, secrets: Mapping[str, SecretStr], http: httpx.AsyncClient) -> Deliverer:
    """Pushover in ``pushover`` mode, the log in dry run (AC-13)."""
    if settings.alerts_delivery == "pushover":
        return PushoverDelivery(PushoverClient(http, secrets[PUSHOVER_APP_TOKEN], secrets[PUSHOVER_USER_KEY]))
    return LogDelivery()


def watchdog(settings: AlerterSettings, secrets: Mapping[str, SecretStr], http: httpx.AsyncClient) -> Watchdog | None:
    """The ``alerter-alive`` ping in ``pushover`` mode; none in dry run (AC-13)."""
    if settings.alerts_delivery == "pushover":
        return Watchdog(http, secrets[HC_ALERTER_URL])
    return None


def repository(settings: AlerterSettings, secrets: Mapping[str, SecretStr]) -> AlertRepository:
    """``AlertRepository`` as ``alerter_app``, with a short connect timeout so a down Postgres fails fast."""
    engine = create_engine(
        settings.database_url(secrets[POSTGRES_PASSWORD]),
        pool_size=1,
        max_overflow=1,
        pool_pre_ping=True,
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S},
    )
    return AlertRepository(sessionmaker(engine))


def redis_client(settings: AlerterSettings, password: SecretStr) -> Redis:
    """The redis-py client as the ``alerter`` ACL user."""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password.get_secret_value(),
        socket_timeout=REDIS_TIMEOUT_S,
        socket_connect_timeout=REDIS_TIMEOUT_S,
    )


async def serve(started: Boot[AlerterSettings]) -> None:
    """Run the alerter until the process is stopped."""
    settings, secrets = started.settings, started.secrets
    engine = AlertEngine(engine_config(settings))
    store = StoreWriter(repository(settings, secrets), engine, utc_now)
    async with httpx.AsyncClient() as http:
        ping = watchdog(settings, secrets, http)
        alerter = Alerter(
            env=settings.trading_env,
            engine=engine,
            deliverer=deliverer(settings, secrets, http),
            store=store,
            redis=redis_client(settings, secrets[REDIS_PASSWORD]),
            trader_down_s=settings.alerts_trader_down_s,
            watchdog=ping,
        )
        log.info(
            "alerter running",
            extra={
                "fields": {
                    "code": "alerter_started",
                    "delivery": settings.alerts_delivery,
                    "watchdog": ping is not None,
                    "trader_down_s": settings.alerts_trader_down_s,
                }
            },
        )
        await alerter.run()
