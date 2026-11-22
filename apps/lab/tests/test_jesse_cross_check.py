"""covers: spec 0033 AC-11, the Jesse cross check: ``lab book jesse-export`` (Jesse rows with missing minutes filled
flat, the reference with ``return_closed``, exit codes) and the pure parts of ``jesse/cross_check.py`` (Jesse's
fill stamps mapped to pure replay days, the pass rule, missing inputs). Jesse itself runs only in its own
container, so the backtest is not run here.
"""

import csv
import importlib.util
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lab.jesse_export import minute_rows, reference, run_jesse_export
from lab.main import parser
from trading_core.lab.candles import MINUTE_NS

from .candle_fixtures import FEB, HOLE, synthetic_month
from .conftest import lab_settings

_SPEC = importlib.util.spec_from_file_location(
    "cross_check", Path(__file__).resolve().parents[1] / "jesse" / "cross_check.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

D = Decimal
BTC = "PF_XBTUSD.KRAKEN"


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1000


def test_minute_rows_are_keyed_by_open_and_fill_the_hole_flat(tmp_path):
    tree = synthetic_month(tmp_path, days=4)  # the hole is at minute 5000
    stored = {r.ts_ns: r for r in tree.read_bars(FEB)}
    end = FEB.start_ns + 4 * 1440 * MINUTE_NS
    rows, filled = minute_rows(tree, FEB.start_ns - 30 * MINUTE_NS, end)
    # Nothing before the first stored candle, then one row a minute up to the end.
    assert int(rows[0][0]) == FEB.start_ns // 1_000_000
    assert len(rows) == 4 * 1440
    assert filled == len(HOLE)
    by_open = {int(r[0]) * 1_000_000: r for r in rows}
    first = stored[FEB.start_ns + MINUTE_NS]
    assert by_open[FEB.start_ns] == [
        str(FEB.start_ns // 1_000_000),
        str(first.open),
        str(first.close),
        str(first.high),
        str(first.low),
        str(first.volume),
    ]
    before_hole = stored[FEB.start_ns + HOLE[0] * MINUTE_NS].close
    for m in HOLE:
        row = by_open[FEB.start_ns + m * MINUTE_NS]
        assert row[1:] == [str(before_hole)] * 4 + ["0"]


def _reference(days: list[SimpleNamespace]) -> Any:
    """``reference`` over stand in days (only the fields it reads), with 10,000 start equity and 10 bps cost."""
    result: Any = SimpleNamespace(days=days, start_equity=D(10_000))
    return reference(result, BTC, D("0.001"), {"coin": BTC})


def test_reference_takes_the_closing_cost_off_the_last_equity():
    days = [
        SimpleNamespace(
            day=date(2024, 1, 1), positions={BTC: D("0.5")}, equity=D(10_000), traded=1, gross=D(1), fees=D(7)
        ),
        SimpleNamespace(
            day=date(2024, 1, 2), positions={BTC: D("-0.5")}, equity=D(11_000), traded=1, gross=D(1), fees=D(8)
        ),
        SimpleNamespace(day=date(2024, 1, 3), positions={}, equity=D(11_000), traded=0, gross=D(0), fees=D(0)),
    ]
    ref = _reference(days)
    assert [d[:2] for d in ref["days"]] == [["2024-01-01", 1], ["2024-01-02", -1], ["2024-01-03", 0]]
    assert ref["trades"] == 2
    assert ref["fees"] == "15"
    # Flat on the last day, so nothing to close.
    assert D(ref["return_closed"]) == D(ref["return"]) == D("0.1")
    days[-1] = SimpleNamespace(
        day=date(2024, 1, 3), positions={BTC: D(1)}, equity=D(11_000), traded=0, gross=D(1), fees=D(0)
    )
    ref = _reference(days)
    assert D(ref["return_closed"]) == (D(11_000) - D(11)) / D(10_000) - 1


def test_export_writes_both_files(tmp_path, capsys):
    synthetic_month(tmp_path / "catalog", days=10)
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    args = parser().parse_args(["book", "jesse-export", "--coins", BTC, "--from", "2026-02-02", "--to", "2026-02-09"])
    assert run_jesse_export(settings, args) == 0
    folder = tmp_path / "reports" / "jesse" / "PF_XBTUSD_2026-02-02_2026-02-09"
    ref = json.loads((folder / "reference.json").read_text())
    assert ref["inputs"]["lookback_days"] == "30"
    assert ref["inputs"]["minutes_filled"] == str(len(HOLE))
    assert len(ref["days"]) == 8
    # 10 days never reach the minimum history, so the reference never trades.
    assert ref["trades"] == 0
    with (folder / "candles.csv").open() as fh:
        rows = list(csv.reader(fh))
    # Up to the open of the last day: Jesse decides on it at the close of the day before.
    assert int(rows[-1][0]) == _ms(date(2026, 2, 9)) - 60_000
    assert ref["inputs"]["minutes"] == str(len(rows))
    assert "0 trades" in capsys.readouterr().out


def test_export_refuses_bad_input(tmp_path):
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    bad_coin = parser().parse_args(["book", "jesse-export", "--coins", "PF_NOPEUSD.KRAKEN"])
    assert run_jesse_export(settings, bad_coin) == 64
    backwards = parser().parse_args(["book", "jesse-export", "--from", "2024-02-01", "--to", "2024-01-01"])
    assert run_jesse_export(settings, backwards) == 64
    no_data = parser().parse_args(["book", "jesse-export", "--coins", BTC])
    assert run_jesse_export(settings, no_data) == 2


def test_jesse_stamps_map_to_the_pure_replay_days():
    first, last = date(2024, 1, 1), date(2024, 1, 10)
    trades = [
        # Opened at the close of Dec 31 (stamped Jan 1), flipped at the close of Jan 4 (stamped Jan 5).
        cc.Trade(1, _ms(date(2024, 1, 1)), _ms(date(2024, 1, 5)), False),
        cc.Trade(-1, _ms(date(2024, 1, 5)), _ms(date(2024, 1, 8)), False),
        # Opened again Jan 9, closed only because the backtest ended.
        cc.Trade(1, _ms(date(2024, 1, 9)), _ms(date(2024, 1, 11)), True),
    ]
    sides = cc.daily_sides(trades, first, last)
    assert [sides[date(2024, 1, d)] for d in range(1, 11)] == [1, 1, 1, 1, -1, -1, -1, 0, 1, 1]


def test_the_pass_rule():
    days = [["2024-01-01", 1, "1", 1], ["2024-01-02", -1, "1", 1]]
    ref = {"days": days, "return_closed": "0.10"}
    good = {date(2024, 1, 1): 1, date(2024, 1, 2): -1}
    assert cc.compare(ref, good, D("0.115")).passed
    gap = cc.compare(ref, good, D("0.125"))
    assert not gap.passed
    assert gap.gap_pp == D("2.500")
    miss = cc.compare(ref, {date(2024, 1, 1): 1}, D("0.10"))
    assert not miss.passed
    assert miss.side_misses == [("2024-01-02", -1, 0)]


def test_missing_inputs_exit_64(tmp_path, capsys):
    assert cc.main([str(tmp_path)]) == 64
    assert "no jesse-export inputs" in capsys.readouterr().err
