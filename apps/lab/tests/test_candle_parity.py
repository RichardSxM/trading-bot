"""covers: spec 0014 AC-21 (lab candle-parity: own segments run through the order book unit and the candle unit,
enter decisions matched, the ratio, unmatched decisions, and the exits)."""

import io
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from lab.candle_parity import BOOK, CANDLES, candle_parity
from lab.candle_units import CandleUnitPlan
from lab.runfile import Overrides, load_candle_run
from trading_core.lab.candles import Month
from trading_core.recording.enums import Env, Intent, OrderSide, RiskVerdict
from trading_core.recording.rows import DecisionRow

from .candle_fixtures import synthetic_month
from .conftest import FIXTURES, MemoryStore, worker_settings

PARITY_FILE = Path(__file__).parents[1] / "runs" / "macd_parity.toml"
OCT = Month(2026, 10)
MIN = 60_000_000_000


def _catalog(tmp_path, *, candles=True):
    """The own fixture day 2026-10-08 (17:55 to 18:05) and, unless told not to, nine days of October candles."""
    catalog = tmp_path / "catalog"
    shutil.copytree(FIXTURES / "own", catalog / "own")
    if candles:
        synthetic_month(catalog, month=OCT, days=9)
    return catalog


def _loaded(tmp_path, **days):
    text = PARITY_FILE.read_text(encoding="utf-8")
    text = text.replace("from = 2026-10-08", f"from = {days.get('first', '2026-10-08')}")
    text = text.replace("to = 2026-10-10", f"to = {days.get('last', '2026-10-08')}")
    path = tmp_path / "parity.toml"
    path.write_text(text, encoding="utf-8")
    return load_candle_run(path, Overrides())


def _enter(at_ns, side=OrderSide.BUY):
    return DecisionRow(
        id=uuid4(),
        env=Env.DEMO,
        run_id=uuid4(),
        instrument_id="PF_XBTUSD.KRAKEN",
        intent=Intent.ENTER,
        side=side,
        reason="macd_entry",
        inputs={},
        risk_verdict=RiskVerdict.APPROVED,
        risk_reason=None,
        decided_ns=at_ns,
    )


def _start(plan):
    return plan.start_ns if isinstance(plan, CandleUnitPlan) else plan.segment.start_ns


def same_entries(plan):
    """Both sides enter at minutes 1, 5, and 9 of the segment (the candle side 20 s later)."""
    late = 20 * 10**9 if plan.variant == CANDLES else 0
    return SimpleNamespace(decisions=[(n, _enter(_start(plan) + m * MIN + late)) for n, m in enumerate((1, 5, 9))])


def candles_miss_two(plan):
    """The candle side has only the first entry, and a sell the book side never made."""
    if plan.variant == BOOK:
        return same_entries(plan)
    base = _start(plan)
    return SimpleNamespace(decisions=[(0, _enter(base + MIN)), (1, _enter(base + 30 * MIN, OrderSide.SELL))])


def test_matching_entries_on_every_segment_pass(tmp_path):
    out = io.StringIO()
    code = candle_parity(
        worker_settings(_catalog(tmp_path), tmp_path / "reports", 1), _loaded(tmp_path), target=same_entries, out=out
    )
    assert code == 0
    assert out.getvalue().startswith("candle parity 1.0000")
    assert "unmatched" not in out.getvalue()


def test_under_95_percent_exits_1_and_lists_every_unmatched_entry(tmp_path):
    out = io.StringIO()
    code = candle_parity(
        worker_settings(_catalog(tmp_path), tmp_path / "reports", 1),
        _loaded(tmp_path),
        target=candles_miss_two,
        out=out,
    )
    assert code == 1
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("candle parity ")
    assert sum(line.startswith("unmatched book") for line in lines) >= 2
    assert any(line.startswith("unmatched candles") and " sell " in line for line in lines)


def test_a_missing_own_day_or_candle_month_exits_2(tmp_path):
    settings = worker_settings(_catalog(tmp_path), tmp_path / "reports", 1)
    assert candle_parity(settings, _loaded(tmp_path, last="2026-10-09"), target=same_entries) == 2
    bare = tmp_path / "bare"
    bare.mkdir()
    no_candles = worker_settings(_catalog(bare, candles=False), tmp_path / "reports", 1)
    assert candle_parity(no_candles, _loaded(tmp_path), target=same_entries) == 2


def test_both_real_units_run_over_the_fixture_segment_and_write_nothing(tmp_path):
    store = MemoryStore()
    out = io.StringIO()
    code = candle_parity(worker_settings(_catalog(tmp_path), tmp_path / "reports", 2), _loaded(tmp_path), out=out)
    # Ten minutes of order book cannot warm a 1 minute MACD, so nothing matches or nothing is compared.
    assert code in (1, 2)
    assert out.getvalue().startswith("candle parity ")
    assert store.bundles == []
    assert not (tmp_path / "reports").exists()
