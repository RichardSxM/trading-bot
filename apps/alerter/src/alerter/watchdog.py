"""The healthchecks.io ping (spec 0013 AC-2): a missing ping is how a dead alerter or VPS reaches you.

The plain URL says "all well"; ``<url>/fail`` says "running, but pushes are failing" (AC-14). Like
``curl -fsS -m 10 --retry 3``: a 10 s timeout and up to 3 retries.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Final

import httpx
from pydantic import SecretStr

from alerter.pushover import Sleep

log = logging.getLogger(__name__)

TIMEOUT_S: Final = 10.0
RETRY_DELAYS_S: Final = (1.0, 2.0, 4.0)


class Watchdog:
    """Pings one healthchecks.io check."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: SecretStr,
        *,
        delays: Sequence[float] = RETRY_DELAYS_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._url = url.get_secret_value().rstrip("/")
        self._delays = tuple(delays)
        self._sleep = sleep

    async def ping(self, *, failing: bool) -> bool:
        """Ping the check, or its ``/fail`` URL; True when healthchecks.io answered 2xx. Never raises."""
        url = f"{self._url}/fail" if failing else self._url
        for attempt in range(len(self._delays) + 1):
            try:
                response = await self._http.get(url, timeout=TIMEOUT_S)
                if response.is_success:
                    return True
                status: int | None = response.status_code
            except httpx.HTTPError:
                status = None
            if attempt < len(self._delays):
                await self._sleep(self._delays[attempt])
        log.warning("watchdog ping failed", extra={"fields": {"code": "watchdog_ping_failed", "status": status}})
        return False
