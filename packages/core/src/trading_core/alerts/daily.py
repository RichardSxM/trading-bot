"""The daily note (spec 0013 AC-18): when it is due, its fingerprint, and what it says.

Pure: no I/O, no clock. The note is due from ``ALERTS_DAILY_AT`` in ``ALERTS_DAILY_TZ`` until the
end of that local day, so the same rule covers the 08:00 send and the catch up on a later start.
Its ``alerts`` row (rule ``daily``, fingerprint ``daily:<local date>``) is the sent marker.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from trading_core.alerts.model import Tier
from trading_core.alerts.rules import DAILY

WINDOW: Final = timedelta(hours=24)
# How many failed push titles the note names (AC-14: "the daily note lists it").
FAILED_TITLES: Final = 5


def due(now: datetime, at: str, tz: str) -> date | None:
    """The local date whose note is due at ``now``: that date once its ``at`` (``HH:MM``) has passed, else None."""
    local = now.astimezone(ZoneInfo(tz))
    hour, minute = (int(part) for part in at.split(":"))
    return local.date() if local.time() >= time(hour, minute) else None


def fingerprint(day: date) -> str:
    """The note's fingerprint, ``daily:<local date>``."""
    return f"{DAILY.id}:{day.isoformat()}"


def yesterday(now: datetime) -> tuple[datetime, datetime]:
    """Start and end of the UTC day before ``now``'s (the trades and P&L window)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today


def down_seconds(spans: Iterable[tuple[datetime, datetime | None]], now: datetime) -> int:
    """Seconds of the last 24 h covered by ``trader_down`` episodes ``(opened_at, resolved_at)``.

    Each span counts ``min(resolved_at or now, now) - max(opened_at, now - 24 h)``, summed as the
    spec says (episodes of one fingerprint never overlap); a span outside the window counts 0.
    """
    start = now - WINDOW
    total = 0.0
    for opened, resolved in spans:
        end = min(resolved or now, now)
        total += max(0.0, (end - max(opened, start)).total_seconds())
    return int(total)


@dataclass(frozen=True)
class DailyFigures:
    """What the note reports, read from Postgres and Docker.

    ``opened`` counts alerts opened in the last 24 h by tier (daily rows left out). ``pnl`` sums
    the realized P&L of positions closed in the UTC day before, per currency. ``backup`` is the
    backup container's Docker health in plain words.
    """

    opened: Mapping[Tier, int]
    still_open: int
    failed: int
    failed_titles: Sequence[str]
    down_s: int
    trades: int
    pnl: Mapping[str, Decimal] = field(default_factory=dict)
    trade_day: date | None = None
    backup: str = "unknown"


def text(day: date, figures: DailyFigures) -> tuple[str, str]:
    """Title text and body of the note for local date ``day``."""
    opened = ", ".join(f"{figures.opened.get(tier, 0)} {tier.value}" for tier in reversed(Tier))
    lines = [
        f"Last 24 h: {opened} opened. Still open: {figures.still_open}. Failed pushes: {figures.failed}.",
    ]
    if figures.failed_titles:
        named = "; ".join(figures.failed_titles)
        more = figures.failed - len(figures.failed_titles)
        lines.append(f"Failed: {named}" + (f"; and {more} more." if more > 0 else "."))
    lines.append(f"Trader down: {_minutes(figures.down_s)} in the last 24 h.")
    trade_day = "" if figures.trade_day is None else f" (UTC {figures.trade_day.isoformat()})"
    if figures.trades == 0:
        lines.append(f"Yesterday{trade_day}: no trades.")
    else:
        pnl = ", ".join(f"{_signed(amount)} {currency}" for currency, amount in sorted(figures.pnl.items()))
        noun = "trade" if figures.trades == 1 else "trades"
        lines.append(f"Yesterday{trade_day}: {figures.trades} {noun}, realized P&L {pnl}.")
    lines.append(f"Backup: {figures.backup}.")
    return f"Daily note {day.isoformat()}", "\n".join(lines)


def _minutes(seconds: int) -> str:
    return f"{round(seconds / 60)} min"


def _signed(amount: Decimal) -> str:
    return f"+{amount}" if amount > 0 else str(amount)
