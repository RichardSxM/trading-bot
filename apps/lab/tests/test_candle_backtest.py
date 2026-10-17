"""covers: spec 0014 AC-6, AC-11, AC-15, AC-16, AC-17 (lab backtest: run file, range checks, bundle, reports)."""

import json
from decimal import Decimal

import pytest

from lab.candle_backtest import backtest
from lab.candle_fetch import candle_tree
from lab.runfile import Overrides, RunFileError, load_candle_run
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month, MonthManifest
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import FundingSource, RunMode

from .conftest import MemoryStore, lab_settings

D = Decimal
SEP = Month(2026, 9)
DAY = 24 * HOUR_NS
RUN = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2026-09-01
to = 2026-09-01
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"

[strategy]
name = "buy_and_hold"
[strategy.config]
notional_usd = "10000"
"""


def _write_run(tmp_path, text=RUN):
    path = tmp_path / "run.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _day_of_candles(settings, *, funding_hours=range(25)):
    """2026-09-01 as 1440 rising minutes, committed, with hourly funding at the given hours."""
    tree = candle_tree(settings.lab_catalog_dir)
    rows = []
    for m in range(1440):
        o = D(60000 + m)
        rows.append(CandleRow(SEP.start_ns + (m + 1) * MINUTE_NS, o, o + 5, o - 5, o + 1, D(1), False))
    tree.write_bars(SEP, rows)
    funding = [FundingRow(SEP.start_ns + h * HOUR_NS, D("0.0001"), HOUR_NS, "api") for h in funding_hours]
    if funding:
        tree.write_funding(SEP, funding)
    manifest = MonthManifest(
        "2026-09", len(rows), 0, 0, 0, 0, [], rows[0].ts_ns, rows[-1].ts_ns, 0, 0, 0, False, "2026-09-02T00:00:00Z"
    )
    tree.write_manifest(manifest.with_funding(tree.read_funding(SEP)))
    return tree


@pytest.fixture
def settings(tmp_path):
    return lab_settings(tmp_path / "catalog", tmp_path / "reports")


def test_a_day_of_buy_and_hold_writes_one_backtest_bundle_and_its_reports(settings, tmp_path):
    _day_of_candles(settings)
    loaded = load_candle_run(_write_run(tmp_path), Overrides())
    store = MemoryStore()

    assert backtest(settings, loaded, store) == 0

    [bundle] = store.bundles
    assert bundle.run.mode == RunMode.BACKTEST
    config = bundle.run.run_config
    assert (config["source"], config["bar_type"], config["funding_source"]) == (
        "candles",
        "PF_XBTUSD.KRAKEN-1-MINUTE-LAST-EXTERNAL",
        "kraken_export+api",
    )
    assert config["range"] == [SEP.start_ns, SEP.start_ns + DAY]
    assert [d.reason for d in bundle.decisions] == ["buy_and_hold_entry", "range_end"]
    assert [f.side.value for f in bundle.fills] == ["buy", "sell"]
    # Held from 01:00 to the end: funding at 02:00 .. 23:00, each a computed row of its position.
    assert len(bundle.funding) == 22
    assert {f.source for f in bundle.funding} == {FundingSource.COMPUTED}
    [position] = bundle.positions
    assert position.funding_total == sum(f.amount for f in bundle.funding)
    assert bundle.fill_stats == []

    [folder] = list(settings.lab_reports_dir.iterdir())
    report = json.loads((folder / "base.json").read_text())
    candle = report["candle"]
    assert candle["first_decision_ns"] == SEP.start_ns + HOUR_NS
    assert candle["carried_funding_charges"] == 0
    assert candle["months"][0]["month"] == "2026-09"
    assert D(candle["benchmark"]) == (D(61440) - D(60001)) / D(60001)
    # The closed position's row side is FLAT; its direction comes from the opening buy.
    assert (candle["long"]["trades"], candle["short"]["trades"]) == (1, 0)
    assert "Buy and hold" in (folder / "base.md").read_text()
    assert "base" in (folder / "index.md").read_text()


def test_two_runs_give_the_same_report_apart_from_ids_and_times(settings, tmp_path):
    _day_of_candles(settings)
    loaded = load_candle_run(_write_run(tmp_path), Overrides())
    for _ in range(2):
        assert backtest(settings, loaded, MemoryStore()) == 0
    volatile = {"run_id", "batch_id", "started_at", "ended_at"}

    def stable(folder):
        report = json.loads((folder / "base.json").read_text())
        report["run_config"].pop("batch_id")
        return {k: v for k, v in report.items() if k not in volatile}

    first, second = (stable(f) for f in settings.lab_reports_dir.iterdir())
    assert first == second


def test_a_missing_month_or_funding_hole_exits_2_and_writes_nothing(settings, tmp_path):
    store = MemoryStore()
    loaded = load_candle_run(_write_run(tmp_path), Overrides())
    assert backtest(settings, loaded, store) == 2  # no month committed at all

    _day_of_candles(settings, funding_hours=[0, 1, 2, 3, 4, 5, 6])  # a hole from 07:00 on
    assert backtest(settings, loaded, store) == 2
    assert store.bundles == []
    assert not settings.lab_reports_dir.exists() or not any(settings.lab_reports_dir.iterdir())


def test_a_carried_hour_is_charged_and_counted(settings, tmp_path):
    hours = [h for h in range(25) if h != 5]  # 05:00 missing: carried from 04:00, one hour old
    _day_of_candles(settings, funding_hours=hours)
    store = MemoryStore()
    assert backtest(settings, load_candle_run(_write_run(tmp_path), Overrides()), store) == 0
    [folder] = list(settings.lab_reports_dir.iterdir())
    assert json.loads((folder / "base.json").read_text())["candle"]["carried_funding_charges"] == 1
    assert len(store.bundles[0].funding) == 22  # 05:00 is still charged, at the 04:00 rate


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("from = 2026-09-01", "from = 2022-03-23"), "before the first full candle day"),
        (("seed = 7", "seed = 7\nstale_quote_s = 60"), "stale_quote_s"),
        (('name = "buy_and_hold"', 'name = "random_taker"'), "lab backtest runs hour_level"),
        (('notional_usd = "10000"', 'notional_usd = "10000"\nbar_spec = "1-MINUTE-MID"'), "bar_spec"),
        (('notional_usd = "10000"', 'notional_usd = "10000"\n[strategy.grid]\nnotional_usd = ["1", "2"]'), "grid"),
        (('slippage_bps = "2"', 'slippage_bps = "101"'), "slippage_bps"),
    ],
)
def test_the_run_file_is_checked_before_anything_is_read(tmp_path, change, problem):
    with pytest.raises(RunFileError) as caught:
        load_candle_run(_write_run(tmp_path, RUN.replace(*change)), Overrides())
    assert any(problem in p for p in caught.value.problems)
