"""covers: spec 0015 AC-6 as amended for spec 0026 (a ruined candle run is scored, and the lab finds its ruin day)."""

from datetime import date, timedelta
from decimal import Decimal

from lab.study_rows import daily_sharpe, ruin_close
from trading_core.lab.daily import DayClose

START = date(2025, 8, 30)


def _closes(*equity):
    return [DayClose(START + timedelta(days=i), Decimal(e), 1, Decimal(0), Decimal(0)) for i, e in enumerate(equity)]


def test_a_run_closing_below_zero_has_a_sharpe_and_a_ruin_day():
    closes = _closes("400", "-0.0169", "-0.02", "-0.02")

    assert daily_sharpe(closes, Decimal(1000), chained=True) is not None
    assert ruin_close(closes, Decimal(1000)) == closes[1]


def test_a_run_that_stays_above_zero_has_no_ruin_day():
    assert ruin_close(_closes("400", "0.01", "900"), Decimal(1000)) is None
