"""Sample data for ``/styleguide`` (spec 0004 AC-16): every token and component state, never real data."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from markupsafe import Markup

from dashboard.data.alerts import AlertLine
from dashboard.data.pnl import PnlFigures
from dashboard.live.alerts import ALERTS_UNAVAILABLE, MuteView, merge_activity
from dashboard.live.position import LOST, CoinRow, PositionView
from dashboard.live.sides import KRAKEN_UNKNOWN, SidesView
from dashboard.live.state import (
    COMMAND_NO_DATA,
    NO_COMMAND,
    UNKNOWN_ENTRY,
    UNKNOWN_RESULT,
    ActivityRow,
    AgeView,
    AlertKind,
    AlertView,
    BlockedReason,
    BlockedView,
    CommandView,
    ReconcileView,
    collapse,
)
from dashboard.ui.format import num, pnl, ts
from dashboard.ui.tokens import color_tokens, contrast_ratio, read_tokens
from dashboard.web.trades import FILL_COLUMNS, PagerView
from trading_core.alerts.model import Delivery, Tier
from trading_core.journal.bounds import parse_range
from trading_core.journal.summary import BalanceLine, CheckStatus, JournalSummary, PnlCheck, Snapshot, Totals
from trading_core.strategy.blocks import PLAIN_WORDS, BlockReason

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
# Alert episodes between the commands (spec 0013 AC-16): each tier, open and resolved, and each delivery word.
ALERT_SAMPLE: Final = tuple(
    AlertLine(n, SAMPLE_TIME - timedelta(minutes=m), Tier(tier), title, resolved, Delivery(delivery))
    for n, (m, tier, title, resolved, delivery) in enumerate(
        (
            (1, "critical", "Stop not accepted", False, "sent"),
            (2, "warning", "Context feed down on recorder", False, "muted"),
            (6, "info", "Resolved: Trader down", True, "dry_run"),
            (12, "warning", "Container unhealthy: recorder", True, "held"),
            (20, "warning", "Book gap long on compactor", False, "failed"),
            (25, "info", "Fill PF_XBTUSD.KRAKEN", True, "capped"),
            (40, "critical", "Disk space low", False, "pending"),
        )
    )
)
ACTIVITY_WITH_ALERTS: Final = tuple(merge_activity(ACTIVITY_SAMPLE[:3], ALERT_SAMPLE))
ACTIVITY_ALERTS_DOWN: Final = tuple(merge_activity(ACTIVITY_SAMPLE[:2], None))
assert ACTIVITY_ALERTS_DOWN[0] is ALERTS_UNAVAILABLE
MUTE_SAMPLE: Final = MuteView(SAMPLE_TIME + timedelta(hours=4))
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
# The position card in each state (spec 0006 AC-17), labelled sample data.
SAMPLE_PNL: Final = PnlFigures(
    realized_today=Decimal("-0.42"), realized_all=Decimal("3.18"), fees_today=Decimal("0.51")
)
_FRESH: Final = AgeView("1s", "ok")
# Wanted, Trader, Kraken in each state (spec 0023 AC-18).
SIDES_SAMPLES: Final[dict[str, SidesView]] = {
    "agree": SidesView("hold", "long 0.0011", "long 0.0011", "12s", "agree"),
    "disagree": SidesView("flatten submitted", "long 0.0011", "flat", "4s", "disagree"),
    "unknown": SidesView("hold", "long 0.0011", KRAKEN_UNKNOWN, None, "unknown"),
}
_OPEN_BTC: Final = CoinRow(
    "open",
    "PF_XBTUSD.KRAKEN",
    "BTC",
    side="long",
    quantity=Decimal("0.0011"),
    avg_entry=Decimal("64210.50"),
    mark=Decimal("64288.5"),
    stop_px=Decimal("64018"),
    stop_status="accepted",
    unrealized=Decimal("0.09"),
    exit_due_ns=int(SAMPLE_TIME.timestamp()) * 1_000_000_000,
    sides=SIDES_SAMPLES["agree"],
)
_FLAT_BTC: Final = CoinRow("flat", "PF_XBTUSD.KRAKEN", "BTC", mark=Decimal("64288.5"))
_FLAT_PEPE: Final = CoinRow("flat", "PF_PEPEUSD.KRAKEN", "PEPE", mark=Decimal("0.0000123456"))
_UNKNOWN_PEPE: Final = CoinRow("unknown", "PF_PEPEUSD.KRAKEN", "PEPE")
POSITION_SAMPLES: Final[dict[str, PositionView]] = {
    "open": PositionView("live", age=_FRESH, rows=(_OPEN_BTC, _FLAT_PEPE), equity=Decimal("1003.27"), pnl=SAMPLE_PNL),
    "flat": PositionView("live", age=_FRESH, rows=(_FLAT_BTC, _FLAT_PEPE), equity=Decimal("1003.18"), pnl=SAMPLE_PNL),
    "unknown": PositionView("live", age=_FRESH, rows=(_FLAT_BTC, _UNKNOWN_PEPE), pnl=SAMPLE_PNL),
    "row_bad": PositionView(
        "live", age=_FRESH, rows=(_FLAT_BTC, CoinRow("bad", "PF_PEPEUSD.KRAKEN", "PEPE")), pnl=SAMPLE_PNL
    ),
    "offline": PositionView("offline", pnl=SAMPLE_PNL),
    "bad": PositionView("bad", pnl=SAMPLE_PNL),
    "lost": LOST,
    "pnl_unavailable": PositionView("live", age=_FRESH, rows=(_FLAT_BTC,), pnl=None),
}


def _blocked(*reasons: BlockReason) -> BlockedView:
    return BlockedView(tuple(BlockedReason(reason.value, PLAIN_WORDS[reason]) for reason in reasons))


BLOCKED_SAMPLES: Final[dict[str, BlockedView]] = {
    "none": _blocked(),
    "one": _blocked(BlockReason.STALE_FEED),
    "several": _blocked(BlockReason.PAUSED, BlockReason.RECORDING_UNHEALTHY, BlockReason.STOP_NOT_ACCEPTED),
}
CHECK_SAMPLES: Final[dict[str, ReconcileView]] = {
    "clean": ReconcileView("clean · 12s", "ok"),
    "checking": ReconcileView("checking", "warn"),
    "mismatch": ReconcileView("mismatch", "bad"),
    "halted": ReconcileView("halted", "bad"),
    "unknown": ReconcileView("Kraken unknown", "bad"),
    "lost": ReconcileView("no data", "lost"),
}
_JOURNAL_RANGE: Final = parse_range("2026-10-01", "2026-10-29", "paper", date(2026, 10, 30))
_JOURNAL_RUN: Final = UUID("01a18cf3-361b-75f1-95d4-7f70591d723a")
_JOURNAL_USD: Final = Totals(trading=Decimal("14.2100"), fees=Decimal("-1.6800"), funding=Decimal("-0.0300"))
_JOURNAL_EUR: Final = Totals(trading=Decimal("13.08"), fees=Decimal("-1.55"), funding=Decimal("-0.03"))
_JOURNAL_SNAPSHOT: Final = Snapshot(
    "KRAKEN-001", "USD", Decimal("1000.25"), datetime(2026, 9, 30, 21, tzinfo=UTC), _JOURNAL_RUN
)


def _journal(
    status: CheckStatus, reason: str = "", dashboard_net: str = "12.5000", crossing: int = 0, unlinked: int = 0
) -> JournalSummary:
    check = PnlCheck(
        status=status,
        reason=reason,
        journal_net=_JOURNAL_USD.net,
        dashboard_net=Decimal(dashboard_net),
        tolerance=Decimal("0.02"),
        counted=2,
        crossing=crossing,
        unlinked_rows=unlinked,
        unlinked_net=Decimal("-0.0100") if unlinked else Decimal(0),
    )
    return JournalSummary(
        journal_range=_JOURNAL_RANGE,
        rows=8,
        native={"USD": _JOURNAL_USD},
        eur=_JOURNAL_EUR,
        balance_start=[BalanceLine("KRAKEN-001", "USD", _JOURNAL_SNAPSHOT, Decimal("920.69"))],
        balance_end=[BalanceLine("KRAKEN-001", "USD", None, None)],
        check=check,
    )


# The journal Check panel in each state (spec 0024 AC-1, AC-5, AC-9), labelled sample data.
JOURNAL_SAMPLES: Final[dict[str, dict[str, object]]] = {
    "match": {"state": "match", "summary": _journal(CheckStatus.MATCH)},
    "mismatch": {"state": "mismatch", "summary": _journal(CheckStatus.MISMATCH, dashboard_net="11.9000")},
    "not_comparable": {"state": "not_comparable", "summary": _journal(CheckStatus.NOT_COMPARABLE, "edge", crossing=1)},
    "unlinked": {
        "state": "not_comparable",
        "summary": _journal(CheckStatus.NOT_COMPARABLE, "unlinked", unlinked=1),
    },
    "refused": {
        "state": "refused",
        "summary": None,
        "message": "No ECB rate for 2026-10-29 yet. The alerter fetches it hourly, so try again later.",
    },
    "error": {"state": "error", "summary": None, "message": "The dashboard cannot read Postgres right now."},
}
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
        "pager_samples": {
            "first": PagerView(1, older="#page-2"),
            "middle": PagerView(2, newer="#page-1", older="#page-3"),
            "past_end": PagerView(9, first="#page-1"),
        },
        "candles": candles(),
        "pulse_sample": PULSE_SAMPLE,
        "stale_s": (3, 10),
        "activity_groups": collapse(ACTIVITY_SAMPLE),
        "activity_run": collapse(ACTIVITY_RUN),
        "activity_alerts": collapse(ACTIVITY_WITH_ALERTS),
        "activity_alerts_down": collapse(ACTIVITY_ALERTS_DOWN),
        "mute_sample": MUTE_SAMPLE,
        "command_samples": COMMAND_SAMPLES,
        "position_samples": POSITION_SAMPLES,
        "blocked_samples": BLOCKED_SAMPLES,
        "check_samples": CHECK_SAMPLES,
        "journal_samples": JOURNAL_SAMPLES,
        "sides_samples": SIDES_SAMPLES,
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
