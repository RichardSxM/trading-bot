"""The Trades page (spec 0006 AC-19): its column heads, formatted rows, and pagers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode

from markupsafe import Markup

from dashboard.data.trades import TradesView
from dashboard.ui.format import num, pnl, ts
from trading_core.recording.queries import PAGE_SIZE, FillLine, RoundTrip

# (heading, is_number) for the fills table: the Trades page heads and the styleguide sample.
FILL_COLUMNS: Final = [
    ("Time", False),
    ("Market", False),
    ("Side", False),
    ("Size", True),
    ("Price", True),
    ("Fee", True),
]
# (heading, is_number) for the closed round trips table.
ROUND_TRIP_COLUMNS: Final = [
    ("Opened", False),
    ("Closed", False),
    ("Side", False),
    ("Size", True),
    ("Entry", True),
    ("Exit", True),
    ("Net P&L", True),
    ("Fees", True),
    ("Funding", True),
    ("Exit reason", False),
]

Cell = str | Markup


@dataclass(frozen=True)
class PagerView:
    """Links around one page of a list: ``newer`` and ``older`` neighbours, ``first`` only past the end."""

    page: int
    newer: str | None = None
    older: str | None = None
    first: str | None = None


def _market(instrument_id: str) -> str:
    """The venue symbol without the ``.KRAKEN`` suffix: ``PF_XBTUSD``."""
    return instrument_id.partition(".")[0]


def round_trip_cells(trip: RoundTrip) -> list[Cell]:
    """One table row: net P&L includes funding, fees show as money paid (a loss), blanks are unknown values."""
    return [
        ts(trip.opened_at),
        "" if trip.closed_at is None else ts(trip.closed_at),
        "" if trip.side is None else trip.side.value,
        num(trip.size),
        num(trip.avg_entry_px),
        "" if trip.avg_exit_px is None else num(trip.avg_exit_px),
        pnl(trip.net_pnl),
        pnl(-trip.fees),
        pnl(trip.funding),
        "" if trip.exit_reason is None else trip.exit_reason.value.replace("_", " "),
    ]


def fill_cells(fill: FillLine) -> list[Cell]:
    """One table row under ``FILL_COLUMNS``: time, market, side, size, price, fee (as money paid)."""
    return [
        ts(fill.filled_at),
        _market(fill.instrument_id),
        fill.side.value,
        num(fill.quantity),
        num(fill.price),
        pnl(-fill.fee),
    ]


def pager(param: str, page: int, shown: int, others: Mapping[str, int]) -> PagerView:
    """The pager for the list paged by ``param``; ``others`` keeps the other list's page in every link.

    A full page offers ``older`` (the next one may be empty). A page past the end (nothing shown
    after page 1) offers only ``first``.
    """

    def href(to: int) -> str:
        return "?" + urlencode({**others, param: to})

    if shown == 0 and page > 1:
        return PagerView(page, first=href(1))
    return PagerView(
        page,
        newer=href(page - 1) if page > 1 else None,
        older=href(page + 1) if shown >= PAGE_SIZE else None,
    )


def trades_context(view: TradesView, page: int, fills_page: int) -> dict[str, object]:
    """Everything ``pages/trades.html`` renders."""
    return {
        "round_trip_columns": ROUND_TRIP_COLUMNS,
        "round_trips": [round_trip_cells(t) for t in view.round_trips],
        "round_trip_pager": pager("page", page, len(view.round_trips), {"fills_page": fills_page}),
        "fill_columns": FILL_COLUMNS,
        "fills": [fill_cells(f) for f in view.fills],
        "fill_pager": pager("fills_page", fills_page, len(view.fills), {"page": page}),
    }
