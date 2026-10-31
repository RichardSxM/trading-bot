"""What Overview's position card shows (spec 0006 AC-17), as pure functions of the raw snapshots.

The card reads ``{env}:snapshot:positions`` and ``{env}:snapshot:account``. ``positions`` holds one
entry per coin (spec 0025 AC-21), and the card shows one row per entry. A missing snapshot, or one
older than the heartbeat's red limit, makes the whole card ``offline``, never ``flat``: a dead trader
must not look like a calm one. A coin with no quote yet is ``unknown``; an entry that does not parse is
``bad`` on its row alone. Money arrives as strings and becomes ``Decimal`` here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from dashboard.data.pnl import PnlFigures
from dashboard.live.sides import Holding, SidesView
from dashboard.live.state import AgeView, age_state
from trading_core.snapshot.envelope import SnapshotFormatError, decode

CardStatus = Literal["live", "offline", "bad", "lost"]
RowStatus = Literal["open", "flat", "bad", "unknown"]


@dataclass(frozen=True)
class CoinRow:
    """One coin on the card: its state, the position (when open), and its size unit ``base``.

    ``sides`` is Wanted, Trader, and Kraken for this coin (spec 0023 AC-18), None when Redis is unreadable.
    """

    status: RowStatus
    instrument_id: str | None = None
    base: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    avg_entry: Decimal | None = None
    mark: Decimal | None = None
    stop_px: Decimal | None = None
    stop_status: str | None = None
    unrealized: Decimal | None = None
    exit_due_ns: int | None = None
    currency: str = "USD"
    sides: SidesView | None = None

    @property
    def holding(self) -> Holding | None:
        """What the trader holds on this coin for the Trader column; None while unknown or unreadable."""
        if self.status == "flat":
            return ("flat", Decimal(0))
        if self.status != "open" or self.side is None or self.quantity is None:
            return None
        size = abs(self.quantity)
        return ("flat", Decimal(0)) if size.is_zero() else (self.side, size)


@dataclass(frozen=True)
class PositionView:
    """The card: its status, the snapshot age, one row per coin, equity, and the P&L figures.

    ``pnl`` is None when Postgres did not answer; the live part still shows. ``sides`` is the card's
    Wanted, Trader, and Kraken while it has no rows (offline or bad), None when Redis is unreadable.
    """

    status: CardStatus
    age: AgeView | None = None
    rows: tuple[CoinRow, ...] = ()
    equity: Decimal | None = None
    currency: str = "USD"
    pnl: PnlFigures | None = None
    sides: SidesView | None = None

    @property
    def any_open(self) -> bool:
        """True when any coin holds a position, so the card shows the funding it paid so far."""
        return any(row.status == "open" for row in self.rows)


LOST: Final = PositionView("lost")


def _decimal(data: Mapping[str, object], name: str, required: bool = True) -> Decimal | None:
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SnapshotFormatError(f"{name} is not a decimal string")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        raise SnapshotFormatError(f"{name} is not a decimal: {value!r}") from None
    if not decimal.is_finite():
        raise SnapshotFormatError(f"{name} is not finite")
    return decimal


def _equity(raw_account: str | bytes | None) -> Decimal | None:
    if raw_account is None:
        return None
    try:
        return _decimal(decode(raw_account).data, "equity")
    except SnapshotFormatError:
        return None


def _text(data: Mapping[str, object], name: str) -> str | None:
    value = data.get(name)
    return value if isinstance(value, str) else None


def coin_row(entry: object) -> CoinRow:
    """One ``positions`` entry as a card row; an entry that does not parse is ``bad``."""
    if not isinstance(entry, Mapping):
        return CoinRow("bad")
    instrument_id, base = _text(entry, "instrument_id"), _text(entry, "base")
    side = entry.get("side")
    if side == "unknown":
        return CoinRow("unknown", instrument_id, base)
    try:
        mark = _decimal(entry, "mark_px")
        if side == "flat":
            return CoinRow("flat", instrument_id, base, mark=mark)
        exit_due = entry.get("exit_due_ns")
        currency = entry.get("currency")
        if side not in ("long", "short") or not isinstance(currency, str):
            raise SnapshotFormatError("bad side or currency")
        return CoinRow(
            "open",
            instrument_id,
            base,
            side=side,
            quantity=_decimal(entry, "quantity"),
            avg_entry=_decimal(entry, "avg_entry_px"),
            mark=mark,
            stop_px=_decimal(entry, "stop_px", required=False),
            stop_status=_text(entry, "stop_status"),
            unrealized=_decimal(entry, "unrealized_pnl"),
            exit_due_ns=exit_due if isinstance(exit_due, int) and not isinstance(exit_due, bool) else None,
            currency=currency,
        )
    except SnapshotFormatError:
        return CoinRow("bad", instrument_id, base)


def position_view(
    raw_positions: str | bytes | None,
    raw_account: str | bytes | None,
    pnl: PnlFigures | None,
    now_ns: int,
    amber_s: float,
    red_s: float,
) -> PositionView:
    """The card for the raw snapshots (None when a key is missing) and the latest P&L figures."""
    if raw_positions is None:
        return PositionView("offline", pnl=pnl)
    try:
        snapshot = decode(raw_positions)
    except SnapshotFormatError:
        return PositionView("bad", pnl=pnl)
    age = age_state(now_ns, snapshot.ts_ns, amber_s, red_s)
    if age.level == "bad":
        return PositionView("offline", age=age, pnl=pnl)
    coins = snapshot.data.get("coins")
    if not isinstance(coins, list) or not coins:
        return PositionView("bad", age=age, pnl=pnl)
    rows = tuple(coin_row(entry) for entry in coins)
    return PositionView("live", age=age, rows=rows, equity=_equity(raw_account), pnl=pnl)
