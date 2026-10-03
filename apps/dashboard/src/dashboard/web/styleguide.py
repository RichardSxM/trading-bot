"""Sample data for ``/styleguide`` (spec 0004 AC-16): every token and component state, never real data."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from markupsafe import Markup

from dashboard.live.state import (
    COMMAND_NO_DATA,
    NO_COMMAND,
    UNKNOWN_ENTRY,
    UNKNOWN_RESULT,
    ActivityRow,
    AlertKind,
    AlertView,
    CommandView,
    collapse,
)
from dashboard.ui.format import num, pnl, ts
from dashboard.ui.tokens import color_tokens, contrast_ratio, read_tokens

# The text color each swatch is read against: surfaces carry text, alarms carry white or night.
SWATCH_TEXT: Final = {
    "bg": "text",
    "surface": "text",
    "raised": "text",
    "rule": "text",
    "live": "on-alarm",
    "stop": "on-alarm",
    "on-alarm": "stop",
    "demo": "bg",
}
# (heading, is_number) for the fills table: the Trades page heads and the styleguide sample.
FILL_COLUMNS: Final = [
    ("Time", False),
    ("Market", False),
    ("Side", False),
    ("Size", True),
    ("Price", True),
    ("Fee", True),
]
TYPE_ROLES: Final = ("display", "headline", "title", "body-lg", "body", "label", "caption", "number", "number-lg")
SAMPLE_TIME: Final = datetime(2026, 10, 2, 14, 3, 7, 512_000, tzinfo=UTC)
# Sample Activity rows (labelled sample data on /styleguide only): one of each outcome color.
ACTIVITY_SAMPLE: Final = (
    ActivityRow("stop", SAMPLE_TIME, "awaiting trader", "pending"),
    ActivityRow("stop", SAMPLE_TIME - timedelta(minutes=4), "confirmed", "ok", final=True),
    ActivityRow("stop", SAMPLE_TIME - timedelta(minutes=31), "not handled, expired", "bad"),
    UNKNOWN_ENTRY,
)
# A run of three identical rows, which the feed shows as one line with a count.
ACTIVITY_RUN: Final = (
    *(ActivityRow("stop", SAMPLE_TIME - timedelta(seconds=s), "not handled, expired", "bad") for s in (0, 2, 5)),
    ActivityRow("stop", SAMPLE_TIME - timedelta(minutes=9), "confirmed", "ok", final=True),
)
# The top bar's command state, one per level, each with its age.
COMMAND_SAMPLES: Final[dict[str, CommandView]] = {
    "none": NO_COMMAND,
    "pending": CommandView(
        "stop sent · awaiting trader", "pending", status="awaiting", since=SAMPLE_TIME, age="12s ago"
    ),
    "ok": CommandView("stop sent · confirmed", "ok", final=True, status="executed", since=SAMPLE_TIME, age="3m ago"),
    "warn": UNKNOWN_RESULT,
    "bad": CommandView("stop sent · not handled, expired", "bad", status="unhandled", since=SAMPLE_TIME, age="2m ago"),
    "lost": COMMAND_NO_DATA,
}
# The alert bar in each of its states (AC-21).
ALERT_KINDS: Final[tuple[tuple[AlertKind, str], ...]] = (
    ("stop_unhandled", "bad"),
    ("stop_failed", "bad"),
    ("stop_late", "bad"),
    ("stop_unknown", "warn"),
    ("trader_silent", "bad"),
    ("stop_unacknowledged", "warn"),
    ("no_data", "warn"),
)
WINTER_TIME: Final = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)
# Sample heartbeat ages for the tape, one a second; -1 is a second without a live link.
PULSE_SAMPLE: Final = (
    [1, 0, 1, 1, 2, 1, 0, 1, 1, 1] * 5
    + [2, 3, 4, 5, 6, 4, 2, 1, 1, 0]
    + [1, 1, 0, 1, 1, 1, 2, 1, 1, 1] * 2
    + [-1] * 8
    + [12, 13, 7, 2, 1, 1, 0, 1, 1, 1, 0, 1]
    + [1, 0, 1, 1, 1, 0, 1, 1, 1, 1] * 2
)
# One deterministic walk for the chart: close to close moves in USD.
_MOVES: Final = (12, -8, 25, -30, 5, 18, -12, 7, -20, 15, 22, -5, -16, 9, 31, -11)


def swatches(tokens: Mapping[str, str]) -> list[dict[str, str]]:
    """Each color token with its hex and the contrast of its usual text on it."""
    colors = color_tokens(dict(tokens))
    result = []
    for name, value in colors.items():
        partner = SWATCH_TEXT.get(name, "bg")
        ratio = contrast_ratio(value, colors[partner])
        result.append({"name": name, "value": value, "partner": partner, "ratio": f"{ratio:.2f}"})
    return result


def candles(count: int = 48) -> list[dict[str, str | int]]:
    """Hourly sample candles ending at ``SAMPLE_TIME``, prices as strings (the browser parses them)."""
    start = int(SAMPLE_TIME.timestamp()) // 3600 * 3600 - (count - 1) * 3600
    close = Decimal("64210.5")
    series: list[dict[str, str | int]] = []
    for i in range(count):
        move = Decimal(_MOVES[i % len(_MOVES)]) * Decimal("1.5")
        open_, close = close, close + move
        wick = Decimal(_MOVES[(i * 7) % len(_MOVES)]).copy_abs() / 2
        series.append(
            {
                "time": start + i * 3600,
                "open": str(open_),
                "high": str(max(open_, close) + wick),
                "low": str(min(open_, close) - wick),
                "close": str(close),
            }
        )
    return series


def fills() -> list[list[str | Markup]]:
    """Sample rows for the table: time, market, side, size, price, fee."""
    rows = [
        (SAMPLE_TIME, "PF_XBTUSD", "buy", Decimal("0.0150"), Decimal("64210.5"), Decimal("-0.39")),
        (SAMPLE_TIME.replace(minute=1), "PF_XBTUSD", "sell", Decimal("0.0150"), Decimal("64288.0"), Decimal("-0.39")),
        (SAMPLE_TIME.replace(minute=0), "PF_ETHUSD", "buy", Decimal("0.250"), Decimal("2418.35"), Decimal("-0.24")),
    ]
    return [[ts(t), market, side, num(size), num(price), pnl(fee)] for t, market, side, size, price, fee in rows]


def styleguide_context() -> dict[str, object]:
    """Everything ``pages/styleguide.html`` renders."""
    tokens = read_tokens()
    return {
        "swatches": swatches(tokens),
        "type_roles": TYPE_ROLES,
        "spacing": [name.removeprefix("space-") for name in tokens if name.startswith("space-")],
        "radii": [name.removeprefix("radius-") for name in tokens if name.startswith("radius-")],
        "numbers": {
            "positive": Decimal("1234.50"),
            "negative": Decimal("-42.10"),
            "zero": Decimal("-0.00"),
            "grouped": Decimal("1E+3"),
        },
        "pnls": {"profit": Decimal("312.40"), "loss": Decimal("-87.15"), "zero": Decimal("-0.00")},
        "sample_time": SAMPLE_TIME,
        "winter_time": WINTER_TIME,
        "sample_ns": int(SAMPLE_TIME.timestamp()) * 1_000_000_000,
        "fill_columns": FILL_COLUMNS,
        "fills": fills(),
        "candles": candles(),
        "pulse_sample": PULSE_SAMPLE,
        "stale_s": (3, 10),
        "activity_groups": collapse(ACTIVITY_SAMPLE),
        "activity_run": collapse(ACTIVITY_RUN),
        "command_samples": COMMAND_SAMPLES,
        "kraken_url": "https://demo-futures.kraken.com/",  # the sample alerts link to the demo venue
        "alert_samples": [
            AlertView(
                kind,
                "bad" if level == "bad" else "warn",
                None if kind.startswith(("trader", "no_")) else SAMPLE_TIME,
                SAMPLE_TIME + timedelta(minutes=5) if kind == "stop_unacknowledged" else None,
            )
            for kind, level in ALERT_KINDS
        ],
        "sample_nav": (("/styleguide", "Overview", "activity"), ("#trades", "Trades", "arrow-left-right")),
    }
