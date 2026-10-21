"""covers: spec 0015 AC-18 (the ranking: columns, stage, dashes, empty and error states), AC-19 (study detail,
404, 422), AC-20 (promotion log, 50 per page, 422 below 1), AC-21 (paper progress)."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid7

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from dashboard.data.strategies import StrategiesView
from dashboard.web.strategies import DASH, RANKING_COLUMNS, ranking_cells
from trading_core.recording.gate_queries import LOG_PAGE_SIZE, PaperProgress, PromotionLine, StudyDetail, StudyRanking

from .conftest import log_in, make_app

EVALUATED = StudyRanking(
    study_id=uuid7(),
    study="macd-v1",
    strategy="macd_baseline",
    lab="candle",
    entry_mode=None,
    stage="paper",
    verdict="pass",
    metrics={
        "walk_forward_sharpe": "1.234567",
        "holdout_sharpe": "1.1",
        "buy_and_hold_sharpe": "0.4",
        "holdout_return": "0.0512",
        "holdout_max_drawdown": "0.031",
        "drawdown_basis": "marked",
        "deflated_sharpe": "0.97",
        "pbo": "0.1",
        "holdout_trades": 61,
        "holdout_win_rate": "0.45",
    },
)
DEVELOPING = StudyRanking(
    study_id=uuid7(),
    study="scalp-v1",
    strategy="random_taker",
    lab="replay",
    entry_mode="maker",
    stage="developing",
    verdict=None,
    metrics={},
)


@contextmanager
def _logged_in(redis, clock, read_strategies=lambda: StrategiesView(()), **readers):
    app = make_app(redis, clock, read_strategies=read_strategies, **readers)
    with TestClient(app, follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        yield client


def test_an_evaluated_study_shows_every_metric():
    cells = dict(zip((h for h, _ in RANKING_COLUMNS), ranking_cells(EVALUATED, link=False), strict=True))

    assert cells["Walk forward Sharpe"] == "1.23"
    assert cells["Holdout Sharpe vs buy and hold"] == "1.10 vs 0.40"
    assert cells["Holdout return"] == "5.12 %"
    assert cells["Holdout max drawdown"] == "3.10 % marked"
    assert cells["Holdout trades"] == "61"
    assert cells["Win rate"] == "45.00 %"
    assert cells["Stage"] == "paper"


def test_a_developing_study_shows_dashes_in_every_metric():
    cells = ranking_cells(DEVELOPING)

    assert cells[4:6] == ["developing", DASH]
    assert set(cells[6:]) == {DASH}


def test_the_page_lists_the_rows_in_the_order_given(redis, clock):
    with _logged_in(redis, clock, lambda: StrategiesView([EVALUATED, DEVELOPING])) as client:
        page = client.get("/strategies").text

    assert page.index("macd-v1") < page.index("scalp-v1")
    assert f'href="/strategies/{EVALUATED.study_id}"' in page
    assert "Strategy ranking" in page
    assert re.search(r"never ranked on", page)


def test_no_studies_keeps_the_empty_state(redis, clock):
    with _logged_in(redis, clock) as client:
        page = client.get("/strategies").text

    assert "Backtests and strategy scores appear here." in page


def test_a_failed_read_shows_the_error_state(redis, clock):
    def broken():
        raise OperationalError("SELECT", {}, Exception("down"))

    with _logged_in(redis, clock, broken) as client:
        response = client.get("/strategies")

    assert response.status_code == 200
    assert "Strategies unavailable." in response.text


def test_the_page_needs_a_session(client):
    assert client.get("/strategies").status_code == 303


DETAIL = StudyDetail(ranking=EVALUATED, folds=[], attempts=[], trials=3)


def test_a_study_detail_shows_its_figures_and_trials(redis, clock):
    with _logged_in(redis, clock, read_study=lambda study_id: DETAIL if study_id == EVALUATED.study_id else None) as c:
        page = c.get(f"/strategies/{EVALUATED.study_id}").text
        missing = c.get(f"/strategies/{uuid7()}")
        bad = c.get("/strategies/not-a-uuid")

    assert "3 trials in this study" in page
    assert "Walk forward folds" in page
    assert (missing.status_code, bad.status_code) == (404, 422)


def test_the_promotion_log_pages_and_refuses_page_zero(redis, clock):
    line = PromotionLine(
        decided_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
        strategy="macd_baseline",
        version_id=uuid7(),
        frozen_hash="ab" * 32,
        step="candidate → paper",
        verdict="insufficient",
        decided_by="gate",
        reason_codes=("trade_floor:too_few_trades",),
        notes=None,
    )
    pages = []

    def read(page):
        pages.append(page)
        return [line] * LOG_PAGE_SIZE if page == 1 else []

    with _logged_in(redis, clock, read_promotions=read) as client:
        first = client.get("/strategies/log").text
        zero = client.get("/strategies/log?page=0")

    assert pages == [1]
    assert "abababababab" in first
    assert "trade_floor:too_few_trades" in first
    assert "?page=2" in first
    assert zero.status_code == 422


def test_paper_progress_shows_each_figure_against_its_bound(redis, clock):
    progress = PaperProgress(
        frozen_hash="cd" * 32,
        strategy="placeholder_timed",
        metrics={
            "paper_days": "12.50",
            "paper_trades": 40,
            "paper_return": "0.012",
            "paper_max_drawdown": "0.02",
            "paper_mean_trade_return": "0.0003",
            "holdout_trade_return_lower_bound": "0.0001",
            "fill_rate_drift": None,
            "paper_slippage_bps": "0.8",
            "lab_slippage_bps": "0.5",
        },
        reasons=[],
        min_days=30,
        trade_floor=100,
    )
    with _logged_in(redis, clock, lambda: StrategiesView([], [progress])) as client:
        page = client.get("/strategies").text

    assert "Paper progress" in page
    assert "cdcdcdcdcdcd" in page
    assert "0.03 % vs 0.01 %" in page
    assert "12.50 of 30" in page
    assert "40 of 100" in page
