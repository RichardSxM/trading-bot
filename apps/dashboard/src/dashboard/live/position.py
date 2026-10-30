"""What Overview's position card shows (spec 0006 AC-17), as pure functions of the raw snapshots.

The card reads ``{env}:snapshot:position`` and ``{env}:snapshot:account``. A missing position
snapshot, or one older than the heartbeat's red limit, is ``offline``, never ``flat``: a dead
trader must not look like a calm one. Money arrives as strings and becomes ``Decimal`` here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from dashboard.data.pnl import PnlFigures
from dashboard.live.sides import SidesView
from dashboard.live.state import AgeView, age_state
from trading_core.snapshot.envelope import SnapshotFormatError, decode

CardStatus = Literal["open", "flat", "offline", "bad", "lost"]


@dataclass(frozen=True)
class PositionView:
    """The card: its status, the snapshot age, the position (when open), equity, and the P&L figures.

    ``pnl`` is None when Postgres did not answer; the live part still shows. ``sides`` is Wanted, Trader,
    and Kraken (spec 0023 AC-18), None when Redis is unreadable.
    """

    status: CardStatus
    age: AgeView | None = None
    side: str | None = None
    quantity: Decimal | None = None
    avg_entry: Decimal | None = None
    mark: Decimal | None = None
    stop_px: Decimal | None = None
    stop_status: str | None = None
    unrealized: Decimal | None = None
    exit_due_ns: int | None = None
    equity: Decimal | None = None
    currency: str = "USD"
    pnl: PnlFigures | None = None
    sides: SidesView | None = None


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


def position_view(
    raw_position: str | bytes | None,
    raw_account: str | bytes | None,
    pnl: PnlFigures | None,
    now_ns: int,
    amber_s: float,
    red_s: float,
) -> PositionView:
    """The card for the raw snapshots (None when a key is missing) and the latest P&L figures."""
    if raw_position is None:
        return PositionView("offline", pnl=pnl)
    try:
        snapshot = decode(raw_position)
    except SnapshotFormatError:
        return PositionView("bad", pnl=pnl)
    age = age_state(now_ns, snapshot.ts_ns, amber_s, red_s)
    if age.level == "bad":
        return PositionView("offline", age=age, pnl=pnl)
    data = snapshot.data
    equity = _equity(raw_account)
    try:
        mark = _decimal(data, "mark_px")
        if data.get("side") == "flat":
            return PositionView("flat", age=age, mark=mark, equity=equity, pnl=pnl)
        side = data.get("side")
        exit_due = data.get("exit_due_ns")
        stop_status = data.get("stop_status")
        currency = data.get("currency")
        if side not in ("long", "short") or not isinstance(currency, str):
            raise SnapshotFormatError("bad side or currency")
        return PositionView(
            "open",
            age=age,
            side=side,
            quantity=_decimal(data, "quantity"),
            avg_entry=_decimal(data, "avg_entry_px"),
            mark=mark,
            stop_px=_decimal(data, "stop_px", required=False),
            stop_status=stop_status if isinstance(stop_status, str) else None,
            unrealized=_decimal(data, "unrealized_pnl"),
            exit_due_ns=exit_due if isinstance(exit_due, int) and not isinstance(exit_due, bool) else None,
            equity=equity,
            currency=currency,
            pnl=pnl,
        )
    except SnapshotFormatError:
        return PositionView("bad", age=age, pnl=pnl)
