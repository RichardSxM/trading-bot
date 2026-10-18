"""Public, unauthenticated GETs to Kraken Futures (spec 0014 AC-5, AC-22), with the standard library only.

No credentials exist here or are ever sent. Every request waits ``LAB_KRAKEN_PAUSE_MS`` after the
one before. A timeout, connection error, HTTP 429, or 5xx is retried up to five times, waiting 1, 2,
4, 8, then 16 s; any other 4xx or a body that is not JSON fails at once. Numbers in the JSON body
come back as ``Decimal``, never ``float``.
"""

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlencode

log = logging.getLogger(__name__)

TIMEOUT_S: Final = 30
# The wait before each retry of a request that may succeed later (AC-5).
RETRY_WAITS_S: Final = (1, 2, 4, 8, 16)


class FetchFailedError(Exception):
    """A request failed or its body is not the expected JSON. ``url`` holds no query string."""

    def __init__(self, url: str, status: int | None, reason: str) -> None:
        super().__init__(f"{url}: {status} {reason}")
        self.url = url
        self.status = status
        self.reason = reason


Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


class KrakenHttp:
    """GET JSON from one base URL, pausing between requests."""

    def __init__(
        self,
        base_url: str,
        pause_ms: int,
        *,
        opener: Opener = _open,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.pause_s = pause_ms / 1000
        self._opener = opener
        self._sleep = sleep
        self._requests = 0

    def get_json(self, path: str, query: dict[str, str | int] | None = None) -> Any:
        """The decoded JSON body of ``GET <base><path>?<query>``. Raises ``FetchFailedError``."""
        if self._requests and self.pause_s:
            self._sleep(self.pause_s)
        self._requests += 1
        url = self.base_url + path
        full = f"{url}?{urlencode(query)}" if query else url
        for attempt, wait_s in enumerate((*RETRY_WAITS_S, None), start=1):
            try:
                body = self._read(full)
                break
            except _RetryableError as exc:
                if wait_s is None:
                    raise FetchFailedError(url, exc.status, exc.reason) from None
                log.warning(
                    "lab_fetch_retry",
                    extra={
                        "fields": {
                            "url": url,
                            "status": exc.status,
                            "reason": exc.reason,
                            "attempt": attempt,
                            "wait_s": wait_s,
                        }
                    },
                )
                self._sleep(wait_s)
            except urllib.error.HTTPError as exc:
                raise FetchFailedError(url, exc.code, str(exc.reason)) from None
        try:
            return json.loads(body, parse_float=Decimal)
        except ValueError:
            raise FetchFailedError(url, 200, "body is not JSON") from None

    def _read(self, full: str) -> bytes:
        """One GET. Raises ``_RetryableError`` for a failure worth retrying, ``HTTPError`` for any other."""
        request = urllib.request.Request(full, headers={"Accept": "application/json"})
        try:
            with self._opener(request, TIMEOUT_S) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableError(exc.code, str(exc.reason)) from None
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise _RetryableError(None, type(exc).__name__) from None


class _RetryableError(Exception):
    """A timeout, connection error, 429, or 5xx: the request may succeed if tried again."""

    def __init__(self, status: int | None, reason: str) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason
