"""The Pushover client (spec 0013, *Pushover fields*, AC-14).

One call per push. A network error, a timeout (10 s), HTTP 5xx, or 429 is retried after 5, 15,
30, and 60 s; any other 4xx (a bad token or user key) fails at once.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

import httpx
from pydantic import SecretStr

from trading_core.alerts.model import Push, Tier

log = logging.getLogger(__name__)

MESSAGES_URL: Final = "https://api.pushover.net/1/messages.json"
TIMEOUT_S: Final = 10.0
RETRY_DELAYS_S: Final = (5.0, 15.0, 30.0, 60.0)
# Pushover's ``info`` when the user has no device to send to; the push is accepted but goes nowhere.
NO_DEVICES: Final = "no active devices"

# Critical breaks through Do Not Disturb and repeats every 60 s until acknowledged, for 1 h.
_PRIORITY: Final = {Tier.CRITICAL: 2, Tier.WARNING: 0, Tier.INFO: -1}

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class PushResult:
    """The last attempt's outcome: ``ok``, its HTTP status (``None`` for a network error), and the body."""

    ok: bool
    status: int | None
    body: str
    attempts: int


def fields(push: Push, token: SecretStr, user: SecretStr) -> dict[str, str]:
    """The form fields of one Pushover call."""
    values = {
        "token": token.get_secret_value(),
        "user": user.get_secret_value(),
        "title": push.title,
        "message": push.message,
        "priority": str(_PRIORITY[push.tier]),
        "timestamp": str(int(push.episode.opened_at.timestamp())),
    }
    if push.tier is Tier.CRITICAL:
        values |= {"retry": "60", "expire": "3600", "sound": "persistent"}
    return values


def _retryable(status: int | None) -> bool:
    return status is None or status == 429 or status >= 500


class PushoverClient:
    """Sends pushes to one Pushover user from one application."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: SecretStr,
        user: SecretStr,
        *,
        delays: Sequence[float] = RETRY_DELAYS_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._token = token
        self._user = user
        self._delays = tuple(delays)
        self._sleep = sleep

    async def send(self, push: Push) -> PushResult:
        """Send ``push``, retrying as the module says; never raises for an HTTP or network failure."""
        data = fields(push, self._token, self._user)
        attempt = 0
        while True:
            attempt += 1
            result = await self._once(data, attempt)
            if result.ok or not _retryable(result.status) or attempt > len(self._delays):
                return result
            log.warning(
                "push failed, retrying",
                extra={
                    "fields": {
                        "code": "alert_push_retry",
                        "fingerprint": push.episode.fingerprint,
                        "attempt": attempt,
                        "status": result.status,
                    }
                },
            )
            await self._sleep(self._delays[attempt - 1])

    async def _once(self, data: dict[str, str], attempt: int) -> PushResult:
        try:
            response = await self._http.post(MESSAGES_URL, data=data, timeout=TIMEOUT_S)
        except httpx.HTTPError as exc:
            return PushResult(ok=False, status=None, body=f"{type(exc).__name__}: {exc}", attempts=attempt)
        ok = response.status_code == 200 and _status_one(response)
        return PushResult(ok=ok, status=response.status_code, body=response.text, attempts=attempt)


def _status_one(response: httpx.Response) -> bool:
    """Pushover took the push and has a device to send it to.

    With no device logged in to the user's account it still answers ``status: 1``, with ``info``
    "no active devices to send to": the push reaches nobody, so it counts as failed (AC-14).
    """
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict) or body.get("status") != 1:
        return False
    info = body.get("info")
    return not (isinstance(info, str) and NO_DEVICES in info)
