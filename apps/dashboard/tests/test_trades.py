"""covers: spec 0006 AC-19, the Trades page (two lists, newest first, 50 per page, 422 below page 1,
an empty page past the end with a link back to page 1, behind the login)."""

import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from dashboard.data.trades import TradesView
from dashboard.web.trades import PagerView, pager
from trading_core.recording.enums import OrderSide, PositionSide
from trading_core.recording.queries import PAGE_SIZE, FillLine, RoundTrip
from trading_core.strategy.placeholder import ExitReason

from .conftest import log_in, make_app

AT = datetime(2026, 10, 4, 14, 3, 7, tzinfo=UTC)


TRIP = RoundTrip(
    position_id=uuid7(),
    opened_at=AT,
    closed_at=AT.replace(minute=13),
    side=PositionSide.SHORT,
    size=Decimal("0.0011"),
    avg_entry_px=Decimal("64210.5"),
    avg_exit_px=Decimal("64100"),
    realized_pnl=Decimal("0.09"),
    fees=Decimal("0.0353"),
    currency="USD",
    exit_reason=ExitReason.TIME,
)


def _fill() -> FillLine:
    return FillLine(
        fill_id=uuid7(),
        filled_at=AT,
        instrument_id="PF_XBTUSD.KRAKEN",
        side=OrderSide.SELL,
        quantity=Decimal("0.0011"),
        price=Decimal("64210.5"),
        fee=Decimal("0.0176"),
        fee_currency="USD",
    )


@contextmanager
def _logged_in(redis, clock, read_trades):
    with TestClient(make_app(redis, clock, read_trades=read_trades), follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        yield client


def test_the_trades_page_needs_a_session(client):
    response = client.get("/trades")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Ftrades"


def test_rows_render_with_the_number_filters(redis, clock):
    calls = []

    def read(page, fills_page):
        calls.append((page, fills_page))
        return TradesView(round_trips=[TRIP], fills=[_fill()])

    with _logged_in(redis, clock, read) as client:
        page = client.get("/trades").text

    assert calls == [(1, 1)]
    for text in ("Closed round trips", "Latest fills", ">short<", ">time<", ">PF_XBTUSD<", "64,210.5", "0.0011"):
        assert text in page
    assert 'class="pnl pnl--profit"' in page  # net P&L
    assert "\N{MINUS SIGN}0.0353" in page  # fees as money paid
    assert '<time datetime="2026-10-04T14:13:07Z"' in page


def test_blank_side_and_exit_reason_render_as_empty_cells(redis, clock):
    trip = replace(TRIP, side=None, exit_reason=None, avg_exit_px=None)
    with _logged_in(redis, clock, lambda page, fills_page: TradesView(round_trips=[trip], fills=[])) as client:
        page = client.get("/trades").text

    assert ">None<" not in page
    assert "<td></td>" in page


def test_the_pages_reach_the_reader(redis, clock):
    calls = []

    def read(page, fills_page):
        calls.append((page, fills_page))
        return TradesView(round_trips=[], fills=[])

    with _logged_in(redis, clock, read) as client:
        assert client.get("/trades?page=3&fills_page=2").status_code == 200

    assert calls == [(3, 2)]


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=x", "page=1.5", "fills_page=0", "fills_page=x"])
def test_a_bad_page_is_422(redis, clock, query):
    calls = []
    with _logged_in(redis, clock, lambda page, fills_page: calls.append(1)) as client:
        assert client.get(f"/trades?{query}").status_code == 422
    assert calls == []


def test_a_page_past_the_end_links_back_to_page_1(redis, clock):
    with _logged_in(redis, clock, lambda page, fills_page: TradesView(round_trips=[], fills=[_fill()])) as client:
        page = client.get("/trades?page=9&fills_page=1").text

    assert "Nothing on page 9." in page
    assert 'href="?fills_page=1&amp;page=1">Back to page 1</a>' in page
    assert "Each paper position appears here once it closes." in page


def test_a_postgres_failure_shows_an_error_state_and_is_logged(redis, clock, caplog):
    def read(page, fills_page):
        raise OperationalError("select", {}, Exception("connection refused"))

    with _logged_in(redis, clock, read) as client, caplog.at_level(logging.WARNING):
        response = client.get("/trades")

    assert response.status_code == 200
    assert "Trades unavailable." in response.text
    assert any(getattr(r, "fields", {}).get("code") == "trades_read_failed" for r in caplog.records)


def test_pager_links_keep_the_other_list_page():
    assert pager("page", 1, 3, {"fills_page": 2}).older is None
    full = pager("page", 2, PAGE_SIZE, {"fills_page": 4})
    assert (full.newer, full.older, full.first) == ("?fills_page=4&page=1", "?fills_page=4&page=3", None)
    past = pager("fills_page", 5, 0, {"page": 1})
    assert (past.newer, past.older, past.first) == (None, None, "?page=1&fills_page=1")
    assert pager("page", 1, 0, {"fills_page": 1}) == PagerView(1)  # a lone empty first page: no links
