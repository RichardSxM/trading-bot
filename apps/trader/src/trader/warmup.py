"""The warmup seed fetch of ``donchian_scalp_r1``, ``donchian_scalp_r2``, and ``six_vote_trend`` (spec 0025 AC-7 to
AC-9, AC-20; spec 0026 AC-16; spec 0029 AC-8): Kraken's public 5 minute or 1 hour candles.

One unauthenticated ``GET`` per coin to Kraken production's charts API, before the node exists, so it never runs on
the node's event loop and sends no key. A 5 s timeout per try and at most 3 tries: a timeout, connection
error, 429, or 5xx is retried after 1 s, then 2 s; anything else fails at once. The checks are pure
(``trading_core.strategy.warmup``). Any failure gives that coin an empty seed and one ``warmup_seed_failed``
warning naming the coin, so only that coin warms up from live bars; it never raises out of startup.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Final, Literal

from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.trend_portfolio import DailyClose, close_day
from trading_core.strategy.warmup import SeedRefusedError, parse_candles, seed_bars

log = logging.getLogger("trader")

# Market data always comes from Kraken production (its futures demo closed on 14 July 2026), so this is a
# constant, not a setting. ``{symbol}`` is the instrument id's symbol, such as ``PF_XBTUSD``.
# ``{interval}`` is one of ``INTERVALS_S``.
CHARTS_URL: Final = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/{interval}"
type Interval = Literal["5m", "1h", "1d"]
INTERVALS_S: Final[dict[Interval, int]] = {"5m": 300, "1h": 3600, "1d": 86_400}
# r2's: 3 x the EMA period, so the EMA has settled; the minimum is the EMA period. Outside every version hash.
SEED_BARS: Final = 600
SEED_MIN_BARS: Final = 200
SEED_MARGIN_BARS: Final = 20
TIMEOUT_S: Final = 5.0
RETRY_WAITS_S: Final = (1.0, 2.0)

type Get = Callable[[str, float], bytes]


class SeedFetchError(Exception):
    """The candles could not be fetched; ``reason`` is the code logged with ``warmup_seed_failed``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def _get(url: str, timeout_s: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "kfs-trader"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body: bytes = response.read()
        return body


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def fetch_body(
    symbol: str,
    now_s: int,
    get: Get = _get,
    sleep: Callable[[float], None] = time.sleep,
    *,
    interval: Interval = "5m",
    keep_bars: int = SEED_BARS,
) -> bytes:
    """The charts API body for ``symbol``'s ``keep_bars + SEED_MARGIN_BARS`` ``interval`` candles before ``now_s``.

    Raises ``SeedFetchError`` (reason ``fetch_failed``) after the last retry, or at once for an error
    that is not worth retrying.
    """
    start = now_s - (keep_bars + SEED_MARGIN_BARS) * INTERVALS_S[interval]
    url = f"{CHARTS_URL.format(symbol=symbol, interval=interval)}?from={start}&to={now_s}"
    waits = iter(RETRY_WAITS_S)
    while True:
        try:
            return get(url, TIMEOUT_S)
        except Exception as exc:
            wait = next(waits, None) if _retryable(exc) else None
            if wait is None:
                raise SeedFetchError("fetch_failed", f"{type(exc).__name__}: {exc}") from exc
            sleep(wait)


def warmup_seed(
    instrument_id: str,
    now_ns: Callable[[], int] = time.time_ns,
    get: Get = _get,
    sleep: Callable[[float], None] = time.sleep,
    *,
    interval: Interval = "5m",
    keep_bars: int = SEED_BARS,
    min_bars: int = SEED_MIN_BARS,
) -> tuple[BarInput, ...]:
    """The coin's latest ``keep_bars`` complete ``interval`` candles, oldest first, or ``()`` after one warning.

    Fewer than ``min_bars`` complete candles is a failure (``too_few``). The defaults are r2's (5m, 600, 200).

    The warning is ``warmup_seed_failed``. ``instrument_id`` is the Nautilus id (``PF_XBTUSD.KRAKEN``);
    its symbol picks the candles. ``now_ns`` (the wall clock, UTC) is read once before the fetch, so a
    candle closing while the request is under way counts as in progress and is dropped.
    """
    symbol = instrument_id.split(".", 1)[0]
    fetched_ns = now_ns()
    try:
        body = fetch_body(symbol, fetched_ns // 1_000_000_000, get, sleep, interval=interval, keep_bars=keep_bars)
        try:
            payload: object = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SeedRefusedError("not_json", str(exc)) from exc
        interval_ns = INTERVALS_S[interval] * 1_000_000_000
        bars = seed_bars(parse_candles(payload), interval_ns, fetched_ns, keep_bars, min_bars)
    except (SeedFetchError, SeedRefusedError) as exc:
        _failed(instrument_id, exc.reason, str(exc))
        return ()
    except Exception as exc:
        # Never out of startup: a seed problem can only delay entries.
        _failed(instrument_id, "unexpected", f"{type(exc).__name__}: {exc}")
        return ()
    log.info(
        "warmup seed fetched",
        extra={
            "fields": {
                "code": "warmup_seed_fetched",
                "instrument": instrument_id,
                "interval": interval,
                "bars": len(bars),
                "last_close_ns": bars[-1].ts_ns,
            }
        },
    )
    return tuple(bars)


def warmup_seeds(
    instrument_ids: Sequence[str], fetch: Callable[[str], tuple[BarInput, ...]] = warmup_seed
) -> dict[str, tuple[BarInput, ...]]:
    """Each coin's seed, fetched one coin after another (AC-20); a failed coin gets ``()`` and the rest still fetch."""
    return {coin: fetch(coin) for coin in instrument_ids}


def _failed(instrument_id: str, reason: str, detail: str) -> None:
    log.warning(
        "warmup seed failed, warming up from live bars",
        extra={
            "fields": {"code": "warmup_seed_failed", "instrument": instrument_id, "reason": reason, "detail": detail}
        },
    )


def daily_seed(instrument_id: str, bars: Sequence[BarInput], today: date) -> tuple[DailyClose, ...]:
    """``trend_portfolio``'s seed (spec 0030 AC-8): each closed daily candle as its UTC day's close.

    A candle's day is its close time minus one day, as the node files live bars. A seed whose last close is
    not from yesterday (``today`` minus one day, UTC) is dropped with one ``seed_stale`` warning, so the coin
    stays not eligible until live bars fill it. An empty seed (the fetch already warned) stays empty.
    """
    if not bars:
        return ()
    closes = tuple(DailyClose(close_day(bar.ts_ns), bar.close) for bar in bars)
    if closes[-1].day != today - timedelta(days=1):
        log.warning(
            "daily seed is not up to yesterday, dropped",
            extra={
                "fields": {
                    "code": "seed_stale",
                    "instrument": instrument_id,
                    "last_close_day": closes[-1].day.isoformat(),
                }
            },
        )
        return ()
    return closes
