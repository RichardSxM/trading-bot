"""The pure loss limits (spec 0010 AC-8 to AC-12, AC-14): what counts as a loss, and when it halts or blocks.

``LossTally`` holds the realized figures and counts, keyed by ``(position_ref, opened_ns)`` so a
position counts once even when NETTING reuses its id for every round trip. ``on_opened`` and
``on_closed`` fold one position in (a no op for a key already counted), ``roll`` starts a new UTC
day, and ``evaluate`` adds the open positions' unrealized P&L and answers which limit trips (only
on the edge) and which entry blocks hold. No I/O and no clock: the caller passes ``now``.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trading_core.control.halt import HaltReason
from trading_core.strategy.blocks import BlockReason

type PositionKey = tuple[str, int]
"""A position's identity: ``(position_ref, opened_ns)``, the recorder's own key."""

_NS_PER_S = 1_000_000_000


@dataclass(frozen=True, kw_only=True)
class LossLimits:
    """The thresholds (``RISK_*`` settings): USD losses, the total's start date, the trade cap, the cooldown."""

    daily_usd: Decimal
    total_usd: Decimal
    total_since: date
    max_trades_per_day: int
    cooldown_s: int


@dataclass(frozen=True, kw_only=True)
class LossTally:
    """The realized side of the loss figures for one UTC ``day``, and whether each limit already tripped.

    ``realized_*`` are P&L sums (a loss is negative). ``opened_keys`` are the positions opened
    today (the trade count); ``closed_keys`` the positions closed since ``total_since``.
    """

    day: date
    realized_today: Decimal = Decimal(0)
    realized_total: Decimal = Decimal(0)
    trades_today: int = 0
    opened_keys: frozenset[PositionKey] = field(default_factory=frozenset)
    closed_keys: frozenset[PositionKey] = field(default_factory=frozenset)
    last_loss_closed_at: datetime | None = None
    daily_tripped: bool = False
    total_tripped: bool = False


@dataclass(frozen=True, kw_only=True)
class LimitVerdict:
    """One evaluation: the limit that trips now (None when none newly trips), the blocks, and the figures.

    ``loss_today`` and ``loss_total`` are losses in USD: positive is a loss, negative a profit.
    """

    trip: HaltReason | None
    blocks: frozenset[BlockReason]
    loss_today: Decimal
    loss_total: Decimal
    cooldown_until: datetime | None


def utc_day(moment: datetime) -> date:
    """The UTC date of an aware datetime; raises ``ValueError`` for a naive one."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; loss limits need aware UTC times")
    return moment.astimezone(UTC).date()


def opened_day(key: PositionKey) -> date:
    """The UTC date a position opened, from its key's ``opened_ns``."""
    return datetime.fromtimestamp(key[1] / _NS_PER_S, tz=UTC).date()


def roll(tally: LossTally, now: datetime) -> LossTally:
    """Start a new day at UTC midnight: today's figures and the daily trip reset; the total keeps its keys."""
    today = utc_day(now)
    if today == tally.day:
        return tally
    return replace(
        tally,
        day=today,
        realized_today=Decimal(0),
        trades_today=0,
        opened_keys=frozenset(),
        daily_tripped=False,
    )


def on_opened(tally: LossTally, key: PositionKey) -> LossTally:
    """Count a position opened today toward the trade cap; a key already counted, or not opened today, is a no op."""
    if key in tally.opened_keys or opened_day(key) != tally.day:
        return tally
    return replace(tally, trades_today=tally.trades_today + 1, opened_keys=tally.opened_keys | {key})


def on_closed(
    tally: LossTally, key: PositionKey, realized: Decimal, closed_at: datetime, limits: LossLimits
) -> LossTally:
    """Add a closed position's realized P&L (net of fees) once; a loser also moves the cooldown's start.

    It counts today when it closed today, and in the total when it closed on or after ``total_since``.
    """
    if key in tally.closed_keys:
        return tally
    closed_day = utc_day(closed_at)
    last = tally.last_loss_closed_at
    if realized < 0 and (last is None or closed_at > last):
        last = closed_at
    in_total = closed_day >= limits.total_since
    return replace(
        tally,
        realized_today=tally.realized_today + (realized if closed_day == tally.day else Decimal(0)),
        realized_total=tally.realized_total + (realized if in_total else Decimal(0)),
        closed_keys=tally.closed_keys | {key},
        last_loss_closed_at=last,
    )


def evaluate(tally: LossTally, unrealized: Decimal, limits: LossLimits, now: datetime) -> LimitVerdict:
    """The loss figures with ``unrealized`` (the open positions' P&L at the mid), and what they mean now.

    ``trip`` is set only on the edge: ``total_loss_limit`` when the total loss is at or over its
    limit and the total has not tripped yet (it wins when both trip at once), else
    ``daily_loss_limit`` likewise. ``daily_loss_limit`` blocks while today's limit has tripped (until
    the roll at midnight), ``trade_cap`` once today's trades reach the cap, and ``loss_cooldown``
    until the newest losing close plus ``cooldown_s`` (0 turns it off).
    """
    loss_today = -(tally.realized_today + unrealized)
    loss_total = -(tally.realized_total + unrealized)
    trip: HaltReason | None = None
    if not tally.total_tripped and loss_total >= limits.total_usd:
        trip = HaltReason.TOTAL_LOSS_LIMIT
    elif not tally.daily_tripped and loss_today >= limits.daily_usd:
        trip = HaltReason.DAILY_LOSS_LIMIT
    blocks: set[BlockReason] = set()
    if tally.daily_tripped or trip is HaltReason.DAILY_LOSS_LIMIT:
        blocks.add(BlockReason.DAILY_LOSS_LIMIT)
    if tally.trades_today >= limits.max_trades_per_day:
        blocks.add(BlockReason.TRADE_CAP)
    cooldown_until = None
    if limits.cooldown_s > 0 and tally.last_loss_closed_at is not None:
        until = tally.last_loss_closed_at + timedelta(seconds=limits.cooldown_s)
        if now < until:
            cooldown_until = until
            blocks.add(BlockReason.LOSS_COOLDOWN)
    return LimitVerdict(
        trip=trip, blocks=frozenset(blocks), loss_today=loss_today, loss_total=loss_total, cooldown_until=cooldown_until
    )


def tripped(tally: LossTally, reason: HaltReason) -> LossTally:
    """Mark ``reason``'s limit as tripped, so it does not halt again (today for daily, this process for total)."""
    if reason is HaltReason.DAILY_LOSS_LIMIT:
        return replace(tally, daily_tripped=True)
    if reason is HaltReason.TOTAL_LOSS_LIMIT:
        return replace(tally, total_tripped=True)
    raise ValueError(f"{reason} is not a loss limit")
