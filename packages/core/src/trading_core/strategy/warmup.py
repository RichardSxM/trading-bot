"""The warmup seed's checks (spec 0025 AC-7, AC-8): Kraken's 5 minute candles to ``BarInput`` closes, no I/O.

The trader fetches the payload (``trader.warmup``); this module only judges it. ``parse_candles`` reads the
decoded JSON body ``{"candles": [{"time", "open", "high", "low", "close", "volume"}], "more_candles"}``,
where ``time`` is the candle's open time in ms and every price arrives as text. ``seed_bars`` drops the
candles still in progress at ``now_ns``, checks the rest, and keeps the latest ``keep`` as ``BarInput``s whose
``ts_ns`` is the close time, as live bars use. A seed is refused as a whole with ``SeedRefusedError``: a
missing candle inside it is allowed, since live bars skip empty intervals too.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from trading_core.strategy.bar_rule import BarInput

NS_PER_MS: Final = 1_000_000
_PRICE_KEYS: Final = ("open", "high", "low", "close", "volume")


class SeedRefusedError(ValueError):
    """The seed cannot be used; ``reason`` is the short code the trader logs with ``warmup_seed_failed``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Candle:
    """One candle as Kraken sends it: the open time in ms and decimal prices and volume."""

    open_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _decimal(value: object, key: str) -> Decimal:
    if not isinstance(value, str):
        raise SeedRefusedError("bad_decimal", f"{key} is {type(value).__name__}, not text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise SeedRefusedError("bad_decimal", f"{key} {value!r}") from exc
    if not number.is_finite():
        raise SeedRefusedError("bad_decimal", f"{key} {value!r}")
    return number


def parse_candles(payload: object) -> list[Candle]:
    """The candles of a decoded charts API body, in the order sent.

    Raises ``SeedRefusedError`` with reason ``bad_payload`` for any other shape, ``more_candles`` when the
    window did not fit one response, and ``bad_decimal`` for a price or volume that is not decimal text.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise SeedRefusedError("bad_payload", "no candles list")
    more = payload.get("more_candles", False)
    if not isinstance(more, bool):
        raise SeedRefusedError("bad_payload", "more_candles is not a bool")
    if more:
        raise SeedRefusedError("more_candles", "the window did not fit one response")
    candles: list[Candle] = []
    for item in payload["candles"]:
        if not isinstance(item, dict):
            raise SeedRefusedError("bad_payload", "a candle is not an object")
        open_ms = item.get("time")
        if not isinstance(open_ms, int) or isinstance(open_ms, bool):
            raise SeedRefusedError("bad_payload", f"time {open_ms!r}")
        o, h, lo, c, v = (_decimal(item.get(key), key) for key in _PRICE_KEYS)
        candles.append(Candle(open_ms, o, h, lo, c, v))
    return candles


def seed_bars(candles: list[Candle], interval_ns: int, now_ns: int, keep: int, minimum: int) -> list[BarInput]:
    """The latest ``keep`` complete candles as bar closes, oldest first.

    A candle is complete when its close time (open plus ``interval_ns``) is at or before ``now_ns``. Raises
    ``SeedRefusedError`` with reason ``bad_range`` (high below low, or a close outside them),
    ``not_increasing`` (open times not strictly increasing), ``off_boundary`` (an open time not on an
    ``interval_ns`` UTC boundary), or ``too_few`` (fewer than ``minimum`` complete candles).
    """
    bars: list[BarInput] = []
    last_open: int | None = None
    for candle in candles:
        open_ns = candle.open_ms * NS_PER_MS
        if candle.high < candle.low or not candle.low <= candle.close <= candle.high:
            raise SeedRefusedError("bad_range", f"candle at {candle.open_ms}")
        if last_open is not None and open_ns <= last_open:
            raise SeedRefusedError("not_increasing", f"candle at {candle.open_ms}")
        if open_ns % interval_ns:
            raise SeedRefusedError("off_boundary", f"candle at {candle.open_ms}")
        last_open = open_ns
        close_ns = open_ns + interval_ns
        if close_ns > now_ns:
            continue  # still in progress
        bars.append(BarInput(close_ns, candle.open, candle.high, candle.low, candle.close, candle.volume))
    if len(bars) < minimum:
        raise SeedRefusedError("too_few", f"{len(bars)} complete candles, {minimum} needed")
    return bars[-keep:]
