"""Daily closes from Kraken Futures' public charts API, for Overview's standings (distance to flip, form).

``GET https://futures.kraken.com/api/charts/v1/trade/{symbol}/1d?from={unix s}`` answers
``{"candles": [{"time": <ms at 00:00 UTC>, "close": "<decimal>", ...}, ...]}``, no key needed. Demo symbols
are the live ones, so the live host serves both envs. ``DailyCloses`` caches every coin's closes and asks Kraken
again at most every ``refresh_s``; a failed fetch keeps the last closes it had and logs a warning at most once a
``refresh_s``. The fetch is blocking (``urllib``) and runs in a thread.
"""

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

log = logging.getLogger(__name__)

CHARTS_URL: Final = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/1d?from={start}"
HISTORY_DAYS: Final = 45
REFRESH_S: Final = 900.0
TIMEOUT_S: Final = 5.0
USER_AGENT: Final = "kfs-dashboard/1"

Closes = Mapping[str, Mapping[date, Decimal]]


class CandleFormatError(ValueError):
    """A charts reply that does not hold a ``candles`` list of ``time`` and ``close``."""


def symbol_of(instrument_id: str) -> str:
    """The charts symbol of a Nautilus id: ``PF_XBTUSD.KRAKEN`` → ``PF_XBTUSD``."""
    return instrument_id.split(".", 1)[0]


def parse_candles(payload: bytes) -> dict[date, Decimal]:
    """Each candle's UTC day and close; raises ``CandleFormatError`` on any other shape."""
    try:
        candles = json.loads(payload)["candles"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CandleFormatError("no candles list") from exc
    if not isinstance(candles, list):
        raise CandleFormatError("candles is not a list")
    out: dict[date, Decimal] = {}
    for candle in candles:
        try:
            moment = datetime.fromtimestamp(int(candle["time"]) / 1000, UTC)
            close = Decimal(str(candle["close"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CandleFormatError("candle without time or close") from exc
        if not close.is_finite() or close <= 0:
            raise CandleFormatError("close is not a positive number")
        out[moment.date()] = close
    return out


def fetch_url(url: str) -> bytes:
    """GET ``url`` with a short timeout; raises ``OSError`` (``URLError``) when Kraken does not answer."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        body: bytes = response.read()
    return body


class DailyCloses:
    """The cached daily closes of a set of coins, refreshed at most every ``refresh_s``."""

    def __init__(
        self,
        fetch: Callable[[str], bytes] = fetch_url,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        refresh_s: float = REFRESH_S,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._monotonic = monotonic
        self._refresh_s = refresh_s
        self._closes: dict[str, dict[date, Decimal]] = {}
        self._read_at: float | None = None
        self._wanted: tuple[str, ...] = ()
        self._warned_at: float | None = None

    def _read(self, instruments: Sequence[str]) -> dict[str, dict[date, Decimal]]:
        start = int(self._clock()) - HISTORY_DAYS * 86400
        out: dict[str, dict[date, Decimal]] = {}
        for instrument in instruments:
            url = CHARTS_URL.format(symbol=symbol_of(instrument), start=start)
            try:
                out[instrument] = parse_candles(self._fetch(url))
            except (OSError, CandleFormatError) as exc:
                now = self._monotonic()
                if self._warned_at is None or now - self._warned_at >= self._refresh_s:
                    self._warned_at = now
                    log.warning(
                        "daily closes read failed",
                        extra={
                            "fields": {
                                "code": "daily_closes_failed",
                                "instrument": instrument,
                                "error": type(exc).__name__,
                            }
                        },
                    )
                if instrument in self._closes:
                    out[instrument] = self._closes[instrument]
        return out

    async def get(self, instruments: Sequence[str]) -> Closes | None:
        """Every coin's closes, read again when stale or when the coin list changed; None before any coin was read.

        A coin that failed waits for the next refresh like the rest, so a Kraken outage costs one try per
        ``refresh_s``, never one per poll.
        """
        now = self._monotonic()
        wanted = tuple(instruments)
        fresh = self._read_at is not None and now - self._read_at < self._refresh_s
        if not fresh or wanted != self._wanted:
            self._read_at, self._wanted = now, wanted
            self._closes = await asyncio.to_thread(self._read, list(wanted))
        return self._closes or None
