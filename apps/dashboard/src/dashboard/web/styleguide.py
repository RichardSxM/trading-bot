"""Sample data for ``/styleguide`` (spec 0004 AC-16): every token and component state, never real data."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from markupsafe import Markup

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
        "sample_nav": (("/styleguide", "Overview", "activity"), ("#trades", "Trades", "arrow-left-right")),
    }
