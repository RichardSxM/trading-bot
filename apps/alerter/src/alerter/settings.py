"""The alerter's settings and the secrets it loads, which depend on the delivery mode (spec 0013 AC-20)."""

from trading_core.alerts.engine import EngineConfig
from trading_core.secrets import (
    HC_ALERTER_URL,
    POSTGRES_PASSWORD,
    PUSHOVER_APP_TOKEN,
    PUSHOVER_USER_KEY,
    REDIS_PASSWORD,
)
from trading_core.settings import AlertSettings


class AlerterSettings(AlertSettings):
    """``AlertSettings`` plus nothing yet; the alerter's own knobs join here."""


def alerter_secrets(settings: AlerterSettings) -> tuple[str, ...]:
    """The database and Redis passwords always; Pushover and the ping URL only when really pushing.

    So a missing Pushover file stops the alerter at start in ``pushover`` mode only (AC-20).
    """
    base = (POSTGRES_PASSWORD, REDIS_PASSWORD)
    if settings.alerts_delivery == "pushover":
        return (*base, PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, HC_ALERTER_URL)
    return base


def engine_config(settings: AlerterSettings) -> EngineConfig:
    """The engine's timings from the ``ALERTS_*`` settings."""
    return EngineConfig(
        env=settings.trading_env,
        renotify_s=settings.alerts_renotify_s,
        critical_renotify_s=settings.alerts_critical_renotify_s,
    )
