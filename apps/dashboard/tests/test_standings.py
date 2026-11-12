"""Overview's standings and scoreboard (live.standings) and Kraken's daily candles (data.kraken_daily)."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from dashboard.data.kraken_daily import CandleFormatError, DailyCloses, parse_candles, symbol_of
from dashboard.live.portfolio import PlanRow, PortfolioView
from dashboard.live.position import CoinRow
from dashboard.live.standings import (
    Season,
    SeasonPoint,
    daily_moves,
    form_guide,
    gross_share,
    score_view,
    standing,
    standings_view,
    until_words,
)

TODAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 16, 34, tzinfo=UTC)


def closes(start_px, step, days=45, today=TODAY):
    """Closes rising by ``step`` a day, ending yesterday at ``start_px + step * (days - 1)``."""
    first = today - timedelta(days=days)
    return {first + timedelta(days=i): Decimal(start_px) + Decimal(step) * i for i in range(days)}


def plan_row(coin="PF_XBTUSD.KRAKEN", side="long", qty="1", mark="100", pnl="2", status="open", failed=False):
    position = CoinRow(
        status, coin, coin.split("_")[1][:3], side, Decimal(qty), Decimal(90), Decimal(mark), None, None, Decimal(pnl)
    )
    return PlanRow(coin, position.base or "X", "Long", Decimal("10.00"), None, None, failed, position)


def test_the_flip_price_is_the_close_30_days_before_today_and_trend_is_the_mark_over_it():
    history = closes(100, 0, days=45) | {TODAY - timedelta(days=30): Decimal(80)}
    row = standing(plan_row(mark="100"), history, TODAY)
    assert row.flip_px == Decimal(80)
    assert row.trend_pct == Decimal("25.00")
    assert row.flip_move_pct == Decimal("-20.00")
    assert row.flips is False


def test_a_long_whose_trend_turned_negative_flips_and_a_close_one_is_near():
    history = {TODAY - timedelta(days=d): Decimal(100) + (Decimal(1) if d % 2 else Decimal(-1)) for d in range(1, 46)}
    history[TODAY - timedelta(days=30)] = Decimal(101)
    row = standing(plan_row(mark="100"), history, TODAY)
    assert row.trend_pct is not None
    assert row.trend_pct < 0
    assert row.flips is True
    assert row.near is True  # a 1 % move against daily moves of about 2 %


def test_without_closes_the_row_keeps_position_and_pnl_and_blanks_the_trend():
    row = standing(plan_row(), None, TODAY)
    assert (row.side, row.quantity, row.unrealized) == ("long", Decimal(1), Decimal(2))
    assert (row.trend_pct, row.flip_px, row.moves, row.near, row.flips) == (None, None, None, False, False)
    assert all(day.won is None for day in row.form)


def test_daily_moves_needs_a_full_window():
    assert daily_moves(closes(100, 1, days=10), TODAY) is None
    assert daily_moves(closes(100, 0, days=45), TODAY) == Decimal(0)


def test_the_form_guide_scores_each_day_against_the_side_held_now():
    history = closes(100, 1, days=10)  # every day up
    assert [d.won for d in form_guide(history, TODAY, "long")] == [True] * 7
    assert [d.won for d in form_guide(history, TODAY, "short")] == [False] * 7
    assert [d.won for d in form_guide(history, TODAY, "flat")] == [None] * 7
    assert [d.day for d in form_guide(history, TODAY, "long")][-1] == TODAY - timedelta(days=1)


def test_rows_rank_by_trend_and_the_line_sits_under_the_last_positive_one():
    coins = {
        "PF_AAAUSD.KRAKEN": Decimal(90),  # +11 %
        "PF_BBBUSD.KRAKEN": Decimal(120),  # -17 %
        "PF_CCCUSD.KRAKEN": Decimal(50),  # +100 %
    }
    rows = tuple(plan_row(coin, mark="100") for coin in coins)
    book = PortfolioView("running", rows=rows, next_rebalance=NOW)
    data = {coin: closes(100, 0) | {TODAY - timedelta(days=30): px} for coin, px in coins.items()}
    view = standings_view(book, data, TODAY)
    assert [r.instrument_id for r in view.rows] == ["PF_CCCUSD.KRAKEN", "PF_AAAUSD.KRAKEN", "PF_BBBUSD.KRAKEN"]
    assert view.above == 2
    assert view.has_closes is True
    assert view.closes_day == TODAY - timedelta(days=1)


def test_a_book_that_is_not_running_shows_its_status_only():
    assert standings_view(PortfolioView("lost"), None, TODAY).rows == ()
    assert (
        score_view(
            PortfolioView("not_running"),
            standings_view(PortfolioView("not_running"), None, TODAY),
            None,
            NOW,
            Decimal(5),
        ).equity
        is None
    )


def test_the_scoreboard_compares_today_with_the_last_hour_before_midnight_and_the_season_with_its_start():
    book = PortfolioView(
        "running",
        equity=Decimal("1036.53"),
        gross_leverage=Decimal("1.06"),
        next_rebalance=datetime(2026, 11, 13, 0, 5, tzinfo=UTC),
    )
    season = Season(
        "trend_vol",
        (
            SeasonPoint(datetime(2026, 11, 11, 22, tzinfo=UTC), Decimal("1002.12")),
            SeasonPoint(datetime(2026, 11, 11, 23, tzinfo=UTC), Decimal("1002.14")),
            SeasonPoint(datetime(2026, 11, 12, 0, tzinfo=UTC), Decimal("1000.83")),
        ),
    )
    score = score_view(book, standings_view(book, None, TODAY), season, NOW, Decimal(5))
    assert score.today == Decimal("34.39")
    assert score.season_change == Decimal("34.41")
    assert score.strategy == "trend_vol"
    assert score.until == "7 h 31 min"
    assert gross_share(score) == Decimal("0.212")


@pytest.mark.parametrize(
    ("left", "words"),
    [(timedelta(seconds=30), "now"), (timedelta(minutes=31), "31 min"), (timedelta(hours=2, minutes=5), "2 h 5 min")],
)
def test_until_words(left, words):
    assert until_words(left) == words


def test_kraken_candles_parse_to_utc_days_and_bad_shapes_raise():
    payload = json.dumps(
        {"candles": [{"time": 1791072000000, "close": "86506"}, {"time": 1791158400000, "close": "85754.5"}]}
    ).encode()
    assert parse_candles(payload) == {date(2026, 10, 4): Decimal(86506), date(2026, 10, 5): Decimal("85754.5")}
    for bad in (b"{}", b"[]", b'{"candles": [{"time": 1}]}', b'{"candles": [{"time": 1, "close": "0"}]}', b"nope"):
        with pytest.raises(CandleFormatError):
            parse_candles(bad)
    assert symbol_of("PF_XBTUSD.KRAKEN") == "PF_XBTUSD"


def test_daily_closes_read_once_per_refresh_and_keep_the_last_good_closes():
    calls = []
    good = json.dumps({"candles": [{"time": 1791072000000, "close": "1"}]}).encode()

    def fetch(url):
        calls.append(url)
        if len(calls) > 1:
            raise OSError("down")
        return good

    now = [0.0]
    reader = DailyCloses(fetch=fetch, clock=lambda: 1_800_000_000, monotonic=lambda: now[0], refresh_s=900)
    first = asyncio.run(reader.get(["PF_XBTUSD.KRAKEN"]))
    asyncio.run(reader.get(["PF_XBTUSD.KRAKEN"]))
    assert len(calls) == 1
    now[0] = 1000.0
    again = asyncio.run(reader.get(["PF_XBTUSD.KRAKEN"]))
    assert len(calls) == 2
    assert again == first
    assert "PF_XBTUSD/1d?from=" in calls[0]
