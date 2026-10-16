"""The number and time rules of root DESIGN.md as Jinja filters (spec 0004 AC-17).

Templates never format a number or a time by hand. Money and sizes arrive as ``Decimal`` at the
precision the caller quantized them to; these filters never round.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from markupsafe import Markup

MINUS: Final = "\N{MINUS SIGN}"
NS_PER_S: Final = 1_000_000_000
AMSTERDAM: Final = ZoneInfo("Europe/Amsterdam")


def _finite_decimal(value: object) -> Decimal:
    """``value`` itself when it is a finite ``Decimal``; floats, ints, and bools are refused."""
    if not isinstance(value, Decimal):
        raise TypeError(f"expected a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"cannot show {value}")
    return value


def _magnitude(value: Decimal) -> str:
    """The absolute value at its own precision, comma grouped: ``1,234.50``."""
    return format(value.copy_abs(), ",f")


def num(value: object) -> str:
    """A ``Decimal`` at its own precision, comma grouped, with a true minus sign; ``-0`` shows as ``0``."""
    decimal = _finite_decimal(value)
    text = _magnitude(decimal)
    return MINUS + text if decimal.is_signed() and not decimal.is_zero() else text


def pnl(value: object) -> Markup:
    """A P&L ``Decimal`` with sign, arrow, and the profit or loss class; zero is neutral with neither."""
    decimal = _finite_decimal(value)
    text = _magnitude(decimal)
    if decimal.is_zero():
        return Markup('<span class="pnl pnl--zero">{}</span>').format(text)
    if decimal.is_signed():
        kind, arrow, sign = "loss", "▼", MINUS
    else:
        kind, arrow, sign = "profit", "▲", "+"
    return Markup('<span class="pnl pnl--{}"><span class="pnl__arrow" aria-hidden="true">{}</span>{}{}</span>').format(
        kind, arrow, sign, text
    )


def ts(value: object) -> Markup:
    """A UTC ``<time>`` truncated to seconds, with Amsterdam local time in its ``title``.

    Takes a timezone aware ``datetime`` (a naive one raises ``ValueError``) or nanoseconds since
    the epoch as an ``int``.
    """
    if isinstance(value, bool):
        raise TypeError("expected a datetime or nanoseconds, got bool")
    if isinstance(value, int):
        moment = datetime.fromtimestamp(value // NS_PER_S, UTC)
    elif isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("naive datetime: every time must be timezone aware UTC")
        moment = value.astimezone(UTC).replace(microsecond=0)
    else:
        raise TypeError(f"expected a datetime or nanoseconds, got {type(value).__name__}")
    local = moment.astimezone(AMSTERDAM).strftime("%H:%M:%S %Z")
    return Markup('<time datetime="{}" title="{}, Amsterdam">{}</time>').format(
        moment.strftime("%Y-%m-%dT%H:%M:%SZ"), local, moment.strftime("%Y-%m-%d %H:%M:%SZ")
    )


def local_hm(value: object) -> Markup:
    """An aware ``datetime`` as Amsterdam ``HH:MM`` in a ``<time>``, with the UTC time in its ``title``.

    Only the mute badge shows local time, as spec 0013 AC-16 asks; everything else uses ``ts``.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.utcoffset() is None:
        raise ValueError("naive datetime: every time must be timezone aware UTC")
    moment = value.astimezone(UTC).replace(microsecond=0)
    return Markup('<time datetime="{}" title="{} UTC">{}</time>').format(
        moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        moment.strftime("%H:%M:%SZ"),
        moment.astimezone(AMSTERDAM).strftime("%H:%M"),
    )
