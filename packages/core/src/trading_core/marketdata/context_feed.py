"""The pure rules of the raw Kraken context feed (spec 0009 AC-3 to AC-6).

Kraken's public futures websocket sends ``ticker`` messages with ``openInterest``, and ``trade``
and ``trade_snapshot`` messages whose rows of ``type`` ``liquidation``, ``termination``, or
``block`` are forced or block trades (``fill`` rows are ordinary trades, recorded elsewhere).
Numbers are read as ``Decimal`` straight from the JSON text, never as floats.

The node actor (``trading_core.node.context_feed``) owns the socket and the clock; everything it
decides lives here.
"""

import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from trading_core.marketdata.context import AggressorSide, LiquidationKind
from trading_core.marketdata.hours import SECOND_NS

# The outages file in a recorder run's folder, read by the compactor (AC-6, AC-7).
OUTAGES_FILE: Final = "context_outages.jsonl"
# How many liquidation uids one actor run remembers, so a resubscribe snapshot is not published twice (AC-5).
RECENT_IDS: Final = 10_000
# A reason of ``context_message_bad`` is logged at most once per this long, with the count (AC-4).
BAD_LOG_EVERY_NS: Final = 60 * SECOND_NS
# Connected this long, the backoff starts again from 1 s (AC-6).
BACKOFF_RESET_S: Final = 60
_MS_NS: Final = 1_000_000
_SIDES: Final = {"buy": AggressorSide.BUY, "sell": AggressorSide.SELL}
_KINDS: Final = {kind.value: kind for kind in LiquidationKind}
_FILL: Final = "fill"


def product_id(instrument_id: str) -> str:
    """Kraken's product id for an instrument id: ``PF_XBTUSD.KRAKEN`` is ``PF_XBTUSD``."""
    return instrument_id.split(".", 1)[0]


@dataclass(frozen=True)
class OpenInterest:
    """One ``ticker`` push's open interest, in contracts."""

    product_id: str
    value: Decimal
    ts_event: int


@dataclass(frozen=True)
class ForcedTrade:
    """One ``trade`` row that is not a plain fill."""

    product_id: str
    trade_id: str
    kind: LiquidationKind
    aggressor_side: AggressorSide
    price: Decimal
    size: Decimal
    ts_event: int


@dataclass(frozen=True)
class BadRow:
    """A message or row that was skipped, and why (AC-4)."""

    feed: str
    reason: str


@dataclass(frozen=True)
class Parsed:
    """What one websocket message gave: records to publish, and rows skipped."""

    records: tuple[OpenInterest | ForcedTrade, ...] = ()
    bad: tuple[BadRow, ...] = ()


class _RowError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise _RowError(f"missing_{key}")
    return value


def _number(row: Mapping[str, object], key: str) -> Decimal:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise _RowError(f"missing_{key}" if value is None else f"bad_{key}")
    number = Decimal(value)
    if not number.is_finite():
        raise _RowError(f"bad_{key}")
    return number


def _time_ns(row: Mapping[str, object]) -> int:
    value = row.get("time")
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise _RowError("missing_time" if value is None else "bad_time")
    try:
        ms = int(value)
    except ValueError, InvalidOperation:
        raise _RowError("bad_time") from None
    if ms != value or ms <= 0:
        raise _RowError("bad_time")
    return ms * _MS_NS


def _open_interest(row: Mapping[str, object]) -> OpenInterest | None:
    if "openInterest" not in row:
        return None
    return OpenInterest(_text(row, "product_id"), _number(row, "openInterest"), _time_ns(row))


def _forced_trade(row: Mapping[str, object]) -> ForcedTrade | None:
    kind = row.get("type")
    if kind == _FILL:
        return None
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _RowError("unknown_trade_type")
    side = row.get("side")
    if not isinstance(side, str) or side not in _SIDES:
        raise _RowError("bad_side")
    size = _number(row, "qty")
    price = _number(row, "price")
    if size <= 0 or price <= 0:
        raise _RowError("bad_qty" if size <= 0 else "bad_price")
    return ForcedTrade(
        product_id=_text(row, "product_id"),
        trade_id=_text(row, "uid"),
        kind=_KINDS[kind],
        aggressor_side=_SIDES[side],
        price=price,
        size=size,
        ts_event=_time_ns(row),
    )


def parse_message(text: str | bytes) -> Parsed:
    """The records in one websocket message; events (``info``, ``subscribed``, ...) give none.

    A ``trade_snapshot`` row that fails is skipped alone; the others still count.
    """
    try:
        message = json.loads(text, parse_float=Decimal)
    except ValueError:
        return Parsed(bad=(BadRow("unknown", "bad_json"),))
    if not isinstance(message, dict):
        return Parsed(bad=(BadRow("unknown", "not_an_object"),))
    if "event" in message:
        event = message.get("event")
        if event in ("error", "alert"):
            return Parsed(bad=(BadRow(str(message.get("feed", "unknown")), f"feed_{event}"),))
        return Parsed()
    feed = message.get("feed")
    if feed == "ticker":
        rows: list[object] = [message]
    elif feed == "trade":
        rows = [message]
    elif feed == "trade_snapshot":
        trades = message.get("trades")
        if not isinstance(trades, list):
            return Parsed(bad=(BadRow("trade_snapshot", "missing_trades"),))
        rows = trades
    else:
        return Parsed()
    records: list[OpenInterest | ForcedTrade] = []
    bad: list[BadRow] = []
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise _RowError("not_an_object")
            record = _open_interest(row) if feed == "ticker" else _forced_trade(row)
        except _RowError as exc:
            bad.append(BadRow(str(feed), exc.reason))
            continue
        if record is not None:
            records.append(record)
    return Parsed(tuple(records), tuple(bad))


def subscribe_messages(product_ids: Iterable[str]) -> list[str]:
    """The two subscribe messages; public feeds, so no key and no challenge (AC-3, AC-16)."""
    products = sorted(product_ids)
    return [
        json.dumps({"event": "subscribe", "feed": feed, "product_ids": products}, separators=(",", ":"))
        for feed in ("ticker", "trade")
    ]


class RecentIds:
    """The last ``limit`` ids seen; ``add`` says whether an id is new (AC-5)."""

    def __init__(self, limit: int = RECENT_IDS) -> None:
        self._limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add(self, trade_id: str) -> bool:
        """Remember ``trade_id``; False when it was already among the last ``limit``."""
        if trade_id in self._ids:
            return False
        self._ids[trade_id] = None
        if len(self._ids) > self._limit:
            self._ids.popitem(last=False)
        return True


class BadCounter:
    """Counts skipped rows per ``(feed, reason)`` and says when to log each, once per minute (AC-4)."""

    def __init__(self, every_ns: int = BAD_LOG_EVERY_NS) -> None:
        self._every_ns = every_ns
        self._counts: dict[tuple[str, str], int] = {}
        self._logged: dict[tuple[str, str], int] = {}

    def add(self, row: BadRow, now_ns: int) -> int | None:
        """Count one; returns the count to log (since the last log) when this reason is due, else None."""
        key = (row.feed, row.reason)
        self._counts[key] = self._counts.get(key, 0) + 1
        last = self._logged.get(key)
        if last is not None and now_ns - last < self._every_ns:
            return None
        self._logged[key] = now_ns
        return self._counts.pop(key)


def backoff_s(attempt: int, max_s: int) -> int:
    """The wait before reconnect ``attempt`` (0 based): 1, 2, 4, ... up to ``max_s`` (AC-6)."""
    return min(1 << min(attempt, 30), max_s)


@dataclass(frozen=True)
class Outage:
    """One context feed outage: from the last message before it to the first after it (AC-6)."""

    start_ns: int
    end_ns: int
    reason: str

    def line(self) -> str:
        """The ``context_outages.jsonl`` line, with its newline."""
        payload = {"start_ns": self.start_ns, "end_ns": self.end_ns, "reason": self.reason}
        return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"


def parse_outages(lines: Iterable[str]) -> tuple[list[Outage], int]:
    """The outages in a ``context_outages.jsonl``, and how many lines could not be read.

    A torn last line (a crash mid write) counts as bad, as does any line without the three fields.
    """
    outages: list[Outage] = []
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            start, end, reason = raw["start_ns"], raw["end_ns"], raw["reason"]
        except ValueError, KeyError, TypeError:
            bad += 1
            continue
        if not (isinstance(start, int) and isinstance(end, int) and isinstance(reason, str)) or end < start:
            bad += 1
            continue
        outages.append(Outage(start, end, reason))
    return outages, bad
