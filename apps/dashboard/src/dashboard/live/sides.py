"""The three columns on Overview's position card (spec 0023 AC-18), as a pure function of the raw snapshots.

*Wanted* is ``trader_state.intent`` for a row's coin, *Trader* that coin's entry in the ``positions``
snapshot (the Nautilus cache), and *Kraken* the ``exchange`` snapshot from the last check, with its age.
Each coin row gets its own three (spec 0025 AC-21); a card without rows gets one, for no coin. A missing
exchange snapshot, one with ``status: unknown``, or one older than 3 checks shows ``Kraken unknown``,
never ``flat``. When Trader and Kraken are both known and differ in side or quantity, the card says so.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from dashboard.live.state import short_age
from dashboard.ui.format import num
from trading_core.snapshot.envelope import SnapshotFormatError, decode

NS_PER_S: Final = 1_000_000_000
# The exchange view is unknown once it is older than this many check intervals (spec 0023 AC-18).
STALE_INTERVALS: Final = 3
KRAKEN_UNKNOWN: Final = "Kraken unknown"
UNKNOWN: Final = "unknown"
_INTENT_WORDS: Final = {
    "hold": "hold",
    "entry_submitted": "entry submitted",
    "flatten_submitted": "flatten submitted",
    "stop_replacing": "stop replacing",
}

SidesState = Literal["agree", "disagree", "unknown"]


@dataclass(frozen=True)
class SidesView:
    """Wanted, Trader, and Kraken in plain words; ``state`` is ``unknown`` while Kraken's view is unknown."""

    wanted: str
    trader: str
    kraken: str
    kraken_age: str | None
    state: SidesState


Holding = tuple[str, Decimal]
"""A side (``long``, ``short``, or ``flat``) and its size, always 0 when flat."""


def _data(raw: str | bytes | None) -> tuple[int, Mapping[str, object]] | None:
    if raw is None:
        return None
    try:
        snapshot = decode(raw)
    except SnapshotFormatError:
        return None
    return snapshot.ts_ns, snapshot.data


def _size(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        size = abs(Decimal(value))
    except InvalidOperation:
        return None
    return size if size.is_finite() else None


def _holding(side: object, quantity: object) -> Holding | None:
    if side == "flat":
        return ("flat", Decimal(0))
    size = _size(quantity)
    if side not in ("long", "short") or size is None:
        return None
    return ("flat", Decimal(0)) if size.is_zero() else (str(side), size)


def _words(holding: Holding | None, unknown: str) -> str:
    if holding is None:
        return unknown
    side, size = holding
    return "flat" if side == "flat" else f"{side} {num(size)}"


def _kraken(
    raw_exchange: str | bytes | None, instrument_id: str | None, now_ns: int, max_age_s: float
) -> tuple[Holding | None, str | None]:
    """Kraken's holding on the instrument and the view's age; None when the view is unknown."""
    found = _data(raw_exchange)
    if found is None:
        return None, None
    _, data = found
    fetched_ns = data.get("fetched_ns")
    if data.get("status") != "ok" or not isinstance(fetched_ns, int) or isinstance(fetched_ns, bool):
        return None, None
    age_s = max(0, now_ns - fetched_ns) / NS_PER_S
    if age_s > max_age_s:
        return None, None
    positions = data.get("positions")
    if not isinstance(positions, list):
        return None, None
    holding: Holding = ("flat", Decimal(0))
    for position in positions:
        if not isinstance(position, Mapping):
            return None, None
        if instrument_id is not None and position.get("instrument_id") != instrument_id:
            continue
        found_holding = _holding(position.get("side"), position.get("quantity"))
        if found_holding is None:
            return None, None
        holding = found_holding
    return holding, short_age(age_s)


def _wanted(raw_state: str | bytes | None, instrument_id: str | None) -> str:
    found = _data(raw_state)
    if found is None or instrument_id is None:
        return UNKNOWN
    intents = found[1].get("intent")
    if not isinstance(intents, Mapping):
        return UNKNOWN
    intent = intents.get(instrument_id, "hold")
    return _INTENT_WORDS.get(intent, str(intent)) if isinstance(intent, str) else UNKNOWN


def sides_view(
    instrument_id: str | None,
    trader: Holding | None,
    raw_exchange: str | bytes | None,
    raw_state: str | bytes | None,
    now_ns: int,
    interval_s: float,
) -> SidesView:
    """The three columns for one coin: its holding in the cache, and the raw ``exchange`` and ``trader_state``.

    ``instrument_id`` is None on a card with no rows (offline or bad), and Kraken then shows any position.
    ``trader`` is None while the trader's view is unknown. ``interval_s`` is the check interval: an
    exchange view older than ``STALE_INTERVALS`` of them is unknown.
    """
    kraken, age = _kraken(raw_exchange, instrument_id, now_ns, STALE_INTERVALS * interval_s)
    if kraken is None:
        state: SidesState = "unknown"
    elif trader is not None and trader != kraken:
        state = "disagree"
    else:
        state = "agree"
    return SidesView(
        wanted=_wanted(raw_state, instrument_id),
        trader=_words(trader, UNKNOWN),
        kraken=_words(kraken, KRAKEN_UNKNOWN),
        kraken_age=age,
        state=state,
    )
