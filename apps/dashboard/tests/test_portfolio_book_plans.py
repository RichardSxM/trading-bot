"""covers: spec 0033 AC-8 (the Portfolio page's Rebalances panel: the book run's last rebalances newest first, with
UTC time, reason, annual unit vol in percent, book gross, and per row the orders sent, refused, and coins with no
order in the row's window; the empty state; the read failure; no read without a named run)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import OperationalError

from dashboard.data.portfolio import BookPlanLine, DecisionMark, PlanHead, count_lines

from .test_portfolio import NOW
from .test_portfolio_main import MAIN_RUN, _page, main_snapshots

D = Decimal
BTC, ETH, SOL = "PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN"
H = 3600 * 10**9


def _head(ns, unit_vol="0.0261", gross="1.6234"):
    at = datetime.fromtimestamp(ns // 10**9, UTC)
    return PlanHead(
        uuid.uuid4(), at, ns, "daily", None if unit_vol is None else D(unit_vol), None if gross is None else D(gross)
    )


def test_each_row_counts_the_decisions_of_its_own_window():
    older, newer = _head(10 * H), _head(34 * H)
    coins = {older.id: {BTC, ETH, SOL}, newer.id: {BTC, ETH, SOL}}
    decisions = [
        DecisionMark(10 * H + 1, BTC, "approved"),
        DecisionMark(10 * H + 2, ETH, "rejected"),  # a held grow
        DecisionMark(11 * H, ETH, "approved"),  # its retry, same window
        DecisionMark(34 * H + 5, SOL, "approved"),
        DecisionMark(9 * H, BTC, "approved"),  # before both rows
    ]

    newest, oldest = count_lines([newer, older], coins, decisions, now_ns=40 * H)

    assert (newest.sent, newest.refused, newest.no_order) == (1, 0, 2)
    assert (oldest.sent, oldest.refused, oldest.no_order) == (2, 1, 1)
    assert newest.unit_vol_pct == D("49.9")  # 0.0261 x sqrt 365 = 0.4986
    assert newest.book_gross == D("1.62")


def test_a_book_without_vol_sizing_shows_no_vol():
    [line] = count_lines([_head(H, unit_vol=None, gross=None)], {}, [], now_ns=2 * H)

    assert (line.unit_vol_pct, line.book_gross, line.no_order) == (None, None, 0)


LINES = (
    BookPlanLine(datetime(2026, 11, 7, 0, 5, tzinfo=UTC), "daily", D("49.9"), D("1.62"), 3, 1, 8),
    BookPlanLine(datetime(2026, 11, 6, 0, 5, tzinfo=UTC), "start", None, None, 12, 0, 0),
)


def test_the_panel_lists_the_rows_of_the_run_the_page_names(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())
    asked: list[object] = []

    def read(run):
        asked.append(run)
        return LINES

    page = _page(redis, clock, read_book_plans=read, dashboard_portfolio_slot="main")

    assert asked == [MAIN_RUN]
    panel = page[page.index('id="book-plans-h"') :]
    assert "2026-11-07 00:05:00Z" in panel
    assert panel.index("2026-11-07") < panel.index("2026-11-06")
    assert "49.9 %" in panel
    assert "1.62x" in panel
    assert '<td class="is-num">8</td>' in panel


def test_no_rows_shows_the_empty_state(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())

    page = _page(redis, clock, read_book_plans=lambda run: (), dashboard_portfolio_slot="main")

    assert "No rebalances recorded yet" in page


def test_no_named_run_reads_nothing_and_shows_the_empty_state(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())
    del redis.values["demo:snapshot:trader_state"]
    asked: list[object] = []

    page = _page(redis, clock, read_book_plans=asked.append, dashboard_portfolio_slot="main")

    assert asked == []
    assert "No rebalances recorded yet" in page


def test_a_read_failure_says_so_and_the_page_still_renders(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())

    def broken(run):
        raise OperationalError("select", {}, Exception("down"))

    page = _page(redis, clock, read_book_plans=broken, dashboard_portfolio_slot="main")

    assert "the database did not answer" in page
    assert "Trend portfolio" in page


def test_without_postgres_the_panel_says_so(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())

    page = _page(redis, clock, dashboard_portfolio_slot="main")

    assert "Rebalances" in page
    assert "Unavailable without the database." in page
