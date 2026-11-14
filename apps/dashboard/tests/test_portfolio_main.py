"""covers: spec 0032 AC-2a (the daily schedule's next rebalance), AC-8 (``DASHBOARD_PORTFOLIO_SLOT``: the page reads
main's snapshots and results, and shows main's notional and fees per UTC day)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from dashboard.data.portfolio import DayChurn
from dashboard.live.portfolio import next_rebalance, portfolio_view
from dashboard.settings import DashboardSettings
from trading_core.control.slots import TraderSlot
from trading_core.recording.enums import ControlOutcome
from trading_core.snapshot.envelope import encode

from .conftest import log_in, make_app
from .test_portfolio import NOW, NOW_NS, plan, positions, raw, result_entry

MAIN_RUN = uuid.UUID("01a1b17a-0000-7000-8000-000000000032")
CHURN = (
    DayChurn(date(2026, 11, 7), Decimal("5120.5"), Decimal("2.56")),
    DayChurn(date(2026, 11, 6), Decimal(0), Decimal(0)),
)


def daily_plan(**extra):
    data = {key: value for key, value in plan(failed=()).items() if key != "rebalance_weekday"}
    return {**data, "schedule": "daily", "last_reason": "daily", "close_missing": [], **extra}


def main_snapshots(ts_ns=NOW_NS):
    return {
        "demo:snapshot:heartbeat": encode(ts_ns, {"process": "trader"}),
        "demo:snapshot:trader_state": encode(
            ts_ns, {"paused": False, "halted": False, "run_id": str(MAIN_RUN), "book": True}
        ),
        "demo:snapshot:positions": encode(ts_ns, positions()),
        "demo:snapshot:account": encode(ts_ns, {"equity": "1000"}),
        "demo:snapshot:portfolio_plan": encode(ts_ns, daily_plan()),
    }


def test_the_daily_schedule_rebalances_at_the_next_run_time_on_any_day():
    saturday = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert next_rebalance("00:05", None, saturday, "daily") == datetime(2026, 11, 8, 0, 5, tzinfo=UTC)
    early = datetime(2026, 11, 7, 0, 4, tzinfo=UTC)
    assert next_rebalance("00:05", None, early, "daily") == datetime(2026, 11, 7, 0, 5, tzinfo=UTC)
    assert next_rebalance("00:05", None, early.replace(minute=5), "daily") == datetime(2026, 11, 8, 0, 5, tzinfo=UTC)
    # Without the field the weekday rule is unchanged.
    assert next_rebalance("00:05", 0, saturday) == datetime(2026, 11, 9, 0, 5, tzinfo=UTC)
    assert next_rebalance("00:05", None, saturday) is None


def test_a_daily_plan_shows_the_daily_schedule_and_its_next_run():
    values = {
        "demo:snapshot:portfolio_heartbeat": encode(NOW_NS, {"process": "trader"}),
        "demo:snapshot:portfolio_plan": encode(NOW_NS, daily_plan()),
    }
    shown = portfolio_view(raw(values), None, NOW_NS, 5, 15)
    assert shown.schedule == "daily"
    assert shown.next_rebalance == datetime(2026, 11, 8, 0, 5, tzinfo=UTC)


def test_in_main_mode_the_last_command_is_mains_result_without_a_slot():
    mains, portfolios = uuid.uuid4(), uuid.uuid4()
    results = [
        result_entry(portfolios, ControlOutcome.FAILED, slot="portfolio"),
        result_entry(mains, ControlOutcome.EXECUTED),
    ]
    values = {"demo:snapshot:portfolio_heartbeat": encode(NOW_NS, {"process": "trader"})}

    main = portfolio_view(raw(values, results=results), None, NOW_NS, 5, 15, TraderSlot.MAIN).last_command
    default = portfolio_view(raw(values, results=results), None, NOW_NS, 5, 15).last_command

    assert main is not None
    assert main.level != "bad"
    assert default is not None
    assert default.level == "bad"


def test_the_slot_setting_defaults_to_portfolio_and_refuses_another_value(monkeypatch):
    assert DashboardSettings(trading_env="demo").portfolio_slot is TraderSlot.PORTFOLIO
    monkeypatch.setenv("DASHBOARD_PORTFOLIO_SLOT", "main")
    assert DashboardSettings(trading_env="demo").portfolio_slot is TraderSlot.MAIN
    monkeypatch.setenv("DASHBOARD_PORTFOLIO_SLOT", "other")
    with pytest.raises(ValidationError):
        DashboardSettings(trading_env="demo")


def _page(redis, clock, **kwargs):
    client = TestClient(make_app(redis, clock, **kwargs), follow_redirects=False)
    client.__enter__()
    try:
        assert log_in(client, clock, "/portfolio").status_code == 303
        return client.get("/portfolio").text
    finally:
        client.__exit__(None, None, None)


def test_main_mode_reads_mains_book_its_run_and_its_churn(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())
    asked = []

    def realized(run):
        asked.append(run)
        return Decimal("4.5")

    page = _page(
        redis,
        clock,
        read_portfolio_pnl=realized,
        read_portfolio_churn=lambda: CHURN,
        dashboard_portfolio_slot="main",
    )

    assert asked == [MAIN_RUN]
    board = page[page.index('sse-swap="portfolio-board"') :]
    assert "Portfolio trader not running" not in board
    assert board.count('<th scope="row">') >= 12
    assert "rebalanced every day" in board
    assert "2026-11-08 00:05:00Z" in board
    assert "the main demo trader" in page
    assert "Traded per day" in page
    assert "2026-11-07" in page
    assert "5,120.5 " in page
    assert "2.56 " in page


def test_main_mode_ignores_the_portfolio_slots_keys(redis, clock):
    clock.now = NOW.timestamp()
    del redis.values["demo:snapshot:heartbeat"]  # the fixture's main heartbeat: main is not running here
    redis.values["demo:snapshot:portfolio_heartbeat"] = encode(NOW_NS, {"process": "trader"})

    page = _page(redis, clock, read_portfolio_churn=lambda: CHURN, dashboard_portfolio_slot="main")

    assert "Portfolio trader not running" in page


def test_the_default_mode_shows_no_churn(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())

    page = _page(redis, clock, read_portfolio_churn=lambda: CHURN)

    assert "Traded per day" not in page
    assert "Portfolio trader not running" in page  # main's keys are not read
    assert "its own demo account" in page


def test_a_churn_read_failure_says_so_and_the_page_still_renders(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(main_snapshots())

    def broken():
        raise OperationalError("select", {}, Exception("down"))

    page = _page(redis, clock, read_portfolio_churn=broken, dashboard_portfolio_slot="main")

    assert "Unavailable: the database did not answer" in page
    assert "rebalanced every day" in page
