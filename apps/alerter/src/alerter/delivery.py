"""Where a push goes: Pushover, or the log in dry run (spec 0013 AC-13, AC-14)."""

import logging
from dataclasses import dataclass
from typing import Protocol

from alerter.pushover import PushoverClient
from trading_core.alerts.model import Delivery, Push

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """What became of one push: the delivery state to record and a line for the CLI."""

    delivery: Delivery
    detail: str


class Deliverer(Protocol):
    """Sends one push and says how it went; never raises for a failed push."""

    async def send(self, push: Push) -> Outcome: ...


def _fields(push: Push) -> dict[str, object]:
    return {
        "rule": push.episode.rule,
        "fingerprint": push.episode.fingerprint,
        "tier": push.tier.value,
        "reason": push.reason.value,
        "title": push.title,
        "message": push.message,
    }


class LogDelivery:
    """Dry run: logs the push it would send as ``alert_dry_run`` and sends nothing."""

    async def send(self, push: Push) -> Outcome:
        """Log ``push``; always ``dry_run``."""
        log.info("alert dry run", extra={"fields": {"code": "alert_dry_run", **_fields(push)}})
        return Outcome(Delivery.DRY_RUN, "dry run, logged as alert_dry_run")


class PushoverDelivery:
    """Real pushes through Pushover; a push that fails after its retries is ``failed``."""

    def __init__(self, client: PushoverClient) -> None:
        self._client = client

    async def send(self, push: Push) -> Outcome:
        """Send ``push``; ``sent`` on Pushover's ``status: 1``, else ``failed`` with the reason logged."""
        result = await self._client.send(push)
        if result.ok:
            log.info(
                "alert sent", extra={"fields": {"code": "alert_sent", "attempts": result.attempts, **_fields(push)}}
            )
            return Outcome(Delivery.SENT, result.body)
        log.error(
            "push failed",
            extra={
                "fields": {
                    "code": "alert_push_failed",
                    "status": result.status,
                    "attempts": result.attempts,
                    "response": result.body[:500],
                    **_fields(push),
                }
            },
        )
        return Outcome(Delivery.FAILED, result.body)
