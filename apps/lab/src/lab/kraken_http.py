"""Public, unauthenticated GETs to Kraken Futures (spec 0014 AC-5, AC-22), with the standard library only.

No credentials exist here or are ever sent. Every request waits ``LAB_KRAKEN_PAUSE_MS`` after the
one before. Numbers in the JSON body come back as ``Decimal``, never ``float``.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlencode

TIMEOUT_S: Final = 30


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
        request = urllib.request.Request(full, headers={"Accept": "application/json"})
        try:
            with self._opener(request, TIMEOUT_S) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise FetchFailedError(url, exc.code, str(exc.reason)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchFailedError(url, None, type(exc).__name__) from None
        try:
            return json.loads(body, parse_float=Decimal)
        except ValueError:
            raise FetchFailedError(url, 200, "body is not JSON") from None
