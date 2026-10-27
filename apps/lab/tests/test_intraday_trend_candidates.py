"""covers: spec 0020 AC-2 (each new candidate registered as hour_level with its own module, and how Nautilus
builds 30 minute and 4 hour bars), AC-4 (a bad setting, or the wrong bars, refused before any run), AC-5
(noise_breakout trades through the candle lab on 30 minute bars), AC-7 (donchian_4h trades through the
candle lab on 4 hour bars), AC-8 (each study file keeps the shared settings), AC-12 (donchian_4h_r1, the
revision on 1 hour bars)."""

from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import BarType

from lab.candle_fetch import candle_tree
from lab.candle_stream import CandleStream, SideFlag
from lab.candle_units import CandleUnitPlan, make_candle_engine, run_candle_unit
from lab.runfile import LoadedCandleRun, RunFileError
from lab.strategies import MarketContextValues, StrategyConfigError, registry_entry
from lab.study import load_study
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candles import FundingRow, MonthManifest
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import Intent, StrategyKind
from trading_core.risk.check import RiskLimits

from .candle_fixtures import DAY, FEB, walk

D = Decimal
STUDIES = Path(__file__).resolve().parents[1] / "studies"
SHARED_FROM = "connors_rsi.toml"
DAYS = 10
MINUTE = 60
HALF_HOUR_NS = HOUR_NS // 2
FOUR_HOURS_NS = 4 * HOUR_NS
# A whole 30 minute window (day 2, 08:00 to 08:30) and a whole 4 hour window (day 4, 08:00 to 12:00) with
# no candle, in minutes from the month start.
EMPTY_HALF_HOUR = range(2 * 1440 + 8 * MINUTE, 2 * 1440 + 8 * MINUTE + 30)
EMPTY_FOUR_HOURS = range(4 * 1440 + 8 * MINUTE, 4 * 1440 + 12 * MINUTE)
RISK = RiskLimits(
    max_order_notional=D(20000),
    max_leverage=D(2),
    max_total_exposure=D(20000),
    risk_per_trade=D("0.005"),
    maintenance_margin_rate=D("0.01"),
    min_liq_distance_pct=D(10),
    liq_stop_multiple=D(3),
    stop_slippage_bps=D(5),
)
EXIT_REASONS = {"noise_trail", "time_stop", "stop_through", "range_end", "liquidated"}
DONCHIAN_EXITS = {"channel_exit", "time_stop", "stop_through", "range_end", "liquidated"}


def _plan(root, name, config):
    return CandleUnitPlan(
        variant="base",
        candles_root=root / "candles",
        instrument_id="PF_XBTUSD.KRAKEN",
        start_ns=FEB.start_ns,
        end_ns=FEB.start_ns + DAYS * DAY,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name=name,
        strategy_config=config,
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )


def _month_with_empty_windows(catalog: Path) -> CandleTree:
    """``synthetic_month``'s 10 days, less every candle in the two empty windows."""
    tree = candle_tree(catalog)
    empty = {FEB.start_ns + (m + 1) * MINUTE * 10**9 for m in (*EMPTY_HALF_HOUR, *EMPTY_FOUR_HOURS)}
    rows = [r for r in walk(11, DAYS * 1440, FEB.start_ns) if r.ts_ns not in empty]
    tree.write_bars(FEB, rows)
    tree.write_funding(
        FEB, [FundingRow(FEB.start_ns + h * HOUR_NS, D("0.0001"), HOUR_NS, "api") for h in range(DAYS * 24 + 1)]
    )
    manifest = MonthManifest(
        str(FEB), len(rows), 0, 0, 0, 0, 0, [], rows[0].ts_ns, rows[-1].ts_ns, 0, 0, 0, False, "2026-10-01T00:00:00Z"
    )
    tree.write_manifest(manifest.with_funding(tree.read_funding(FEB)))
    return tree


class _Bars(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Subscribes to internal bar types and keeps every bar it gets: (bar type, close time, volume)."""

    def __init__(self, bar_types):
        super().__init__(ActorConfig(component_id="Bars"))
        self._bar_types = bar_types
        self.bars = []

    def on_start(self):
        for bar_type in self._bar_types:
            self.subscribe_bars(bar_type)

    def on_bar(self, bar):
        self.bars.append((str(bar.bar_type), bar.ts_event, bar.volume.as_decimal()))


def test_nautilus_aligns_30_minute_and_4_hour_bars_to_utc_and_an_empty_window_gives_a_zero_volume_bar(tmp_path):
    tree = _month_with_empty_windows(tmp_path / "catalog")
    half, four = (f"PF_XBTUSD.KRAKEN-{s}-LAST-INTERNAL" for s in ("30-MINUTE", "4-HOUR"))
    plan = _plan(tmp_path / "catalog", "noise_breakout", {})
    probe = _Bars([BarType.from_str(half), BarType.from_str(four)])
    engine = make_candle_engine(plan)
    try:
        engine.add_actor(probe)
        engine.add_data_iterator("candles", CandleStream(tree, plan.start_ns, plan.end_ns, D(2), SideFlag()).chunks())
        engine.run(start=plan.start_ns, end=plan.end_ns)
    finally:
        engine.dispose()

    halves = {ts: volume for kind, ts, volume in probe.bars if kind == half}
    fours = {ts: volume for kind, ts, volume in probe.bars if kind == four}
    assert len(halves) > 400
    assert len(fours) > 50
    assert all(ts % HALF_HOUR_NS == 0 for ts in halves)  # each opens at :00 or :30
    assert all(ts % FOUR_HOURS_NS == 0 for ts in fours)  # each opens at a multiple of 4 hours from midnight
    # What Nautilus does with a window with no trade: it still builds the bar, with volume 0 (spec 0014
    # gate G-3b found the same for hour bars), so the rule tests carry a zero volume case.
    half_close = FEB.start_ns + (EMPTY_HALF_HOUR.stop * MINUTE) * 10**9
    four_close = FEB.start_ns + (EMPTY_FOUR_HOURS.stop * MINUTE) * 10**9
    assert halves[half_close] == 0
    assert fours[four_close] == 0
    assert [ts for ts in range(half_close - 2 * HALF_HOUR_NS, half_close + 2 * HALF_HOUR_NS, HALF_HOUR_NS)] == [
        ts for ts in sorted(halves) if half_close - 2 * HALF_HOUR_NS <= ts < half_close + 2 * HALF_HOUR_NS
    ]  # no bar is skipped around the empty window


def test_noise_breakout_is_an_hour_level_strategy_in_its_own_module():
    entry = registry_entry("noise_breakout")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert entry.module == "trading_core.node.noise_breakout"
    assert {"lookback_days", "band_mult", "bar_spec", "time_stop_bars"} <= entry.user_fields()


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"band_mult": "0"}, "band_mult"),
        ({"lookback_days": 0}, "lookback_days"),
        ({"bar_spec": "15-MINUTE-LAST"}, "bar_spec"),
    ],
)
def test_a_bad_noise_breakout_setting_is_refused_by_the_startup_check(change, problem):
    with pytest.raises(StrategyConfigError, match=problem):
        registry_entry("noise_breakout").check_strategy(change)


def test_a_noise_breakout_study_on_15_minute_bars_is_refused_before_any_run(tmp_path):
    text = (STUDIES / "noise_breakout.toml").read_text(encoding="utf-8")
    path = tmp_path / "quarter.toml"
    path.write_text(text.replace('bar_spec = "30-MINUTE-LAST"', 'bar_spec = "15-MINUTE-LAST"'), encoding="utf-8")
    with pytest.raises(RunFileError, match="bar_spec"):
        load_study(path)


def test_noise_breakout_trades_with_floored_stops_on_30_minute_bars_through_the_candle_lab(tmp_path):
    _month_with_empty_windows(tmp_path / "catalog")
    config = {"lookback_days": 1, "band_mult": "0.1"}
    result = run_candle_unit(_plan(tmp_path / "catalog", "noise_breakout", config))

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    exits = [d for d in decisions if d.intent == Intent.EXIT]
    assert entries
    assert exits
    assert {d.reason for d in entries} <= {"noise_long", "noise_short"}
    assert {d.reason for d in exits} <= EXIT_REASONS
    assert "noise_trail" in {d.reason for d in exits}
    for d in entries:
        mid, atr, stop = (D(str(d.inputs[k])) for k in ("mid", "atr", "stop"))
        assert abs(mid - stop) >= atr  # never closer than 1 ATR to the mid (spec 0018 AC-2)
        assert {"day", "slot", "day_open", "sigma", "vwap", "long_line", "short_line"} <= set(d.inputs)
    assert result.zero_volume_bars >= 1  # the empty 30 minute window
    assert all(p.quantity == 0 for _, p in result.positions)


def _shared(study):
    return {k: v for k, v in study.run_file.items() if k != "strategy"}


def test_the_noise_breakout_study_keeps_the_shared_settings_with_its_3_point_grid():
    reference = load_study(STUDIES / SHARED_FROM)
    study = load_study(STUDIES / "noise_breakout.toml")

    assert _shared(study) == _shared(reference)
    assert (study.section.name, study.section.lab, study.section.seed) == ("noise_breakout", "candle", 42)
    assert isinstance(study.loaded, LoadedCandleRun)
    assert study.loaded.bar_spec == "30-MINUTE-LAST"
    config = dict(study.loaded.run.strategy.config)
    assert (config["time_stop_bars"], config["lookback_days"], config["atr_period"]) == (16, 14, 14)
    assert sorted(D(str(p["band_mult"])) for _, p in study.points) == [D(1), D("1.5"), D(2)]


def test_donchian_4h_is_an_hour_level_strategy_in_its_own_module():
    entry = registry_entry("donchian_4h")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert entry.module == "trading_core.node.donchian_4h"
    assert {"entry_bars", "stop_atr", "bar_spec", "time_stop_bars"} <= entry.user_fields()


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"entry_bars": 1}, "entry_bars"),
        ({"stop_atr": "0.5"}, "stop_atr"),
        ({"bar_spec": "1-HOUR-LAST"}, "bar_spec"),
    ],
)
def test_a_bad_donchian_4h_setting_is_refused_by_the_startup_check(change, problem):
    with pytest.raises(StrategyConfigError, match=problem):
        registry_entry("donchian_4h").check_strategy(change)


def test_a_donchian_4h_study_on_hour_bars_is_refused_before_any_run(tmp_path):
    text = (STUDIES / "donchian_4h.toml").read_text(encoding="utf-8")
    path = tmp_path / "hourly.toml"
    path.write_text(text.replace('bar_spec = "4-HOUR-LAST"', 'bar_spec = "1-HOUR-LAST"'), encoding="utf-8")
    with pytest.raises(RunFileError, match="bar_spec"):
        load_study(path)


def test_donchian_4h_trades_with_floored_stops_on_4_hour_bars_through_the_candle_lab(tmp_path):
    _month_with_empty_windows(tmp_path / "catalog")
    result = run_candle_unit(_plan(tmp_path / "catalog", "donchian_4h", {"entry_bars": 2}))

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    exits = [d for d in decisions if d.intent == Intent.EXIT]
    assert entries
    assert exits
    assert {d.reason for d in entries} <= {"donchian_long", "donchian_short"}
    assert {d.reason for d in exits} <= DONCHIAN_EXITS
    for d in entries:
        mid, atr, stop = (D(str(d.inputs[k])) for k in ("mid", "atr", "stop"))
        assert abs(mid - stop) >= atr  # never closer than 1 ATR to the mid (spec 0018 AC-2)
        assert {"entry_high", "entry_low", "exit_high", "exit_low"} <= set(d.inputs)
    assert result.zero_volume_bars >= 1  # the empty 4 hour window
    assert all(p.quantity == 0 for _, p in result.positions)


def test_the_donchian_4h_study_keeps_the_shared_settings_with_its_4_point_grid():
    reference = load_study(STUDIES / SHARED_FROM)
    study = load_study(STUDIES / "donchian_4h.toml")

    assert _shared(study) == _shared(reference)
    assert (study.section.name, study.section.lab, study.section.seed) == ("donchian_4h", "candle", 42)
    assert isinstance(study.loaded, LoadedCandleRun)
    assert study.loaded.bar_spec == "4-HOUR-LAST"
    config = dict(study.loaded.run.strategy.config)
    assert (config["time_stop_bars"], config["atr_period"]) == (60, 14)
    points = sorted((int(p["entry_bars"]), D(str(p["stop_atr"]))) for _, p in study.points)
    assert points == [(20, D(2)), (20, D(3)), (55, D(2)), (55, D(3))]


# covers: spec 0020 AC-12 and 0020.3 Revision 1 (the same rule on 1 hour bars, in its own module)
def test_donchian_4h_r1_is_an_hour_level_strategy_in_its_own_module():
    entry = registry_entry("donchian_4h_r1")
    assert entry.kind == StrategyKind.HOUR_LEVEL
    assert entry.module == "trading_core.node.donchian_4h_r1"
    assert {"entry_bars", "stop_atr", "bar_spec", "time_stop_bars"} <= entry.user_fields()


def test_a_donchian_4h_r1_setting_on_4_hour_bars_is_refused_by_the_startup_check():
    with pytest.raises(StrategyConfigError, match="bar_spec"):
        registry_entry("donchian_4h_r1").check_strategy({"bar_spec": "4-HOUR-LAST"})


def test_donchian_4h_r1_trades_on_1_hour_bars_through_the_candle_lab(tmp_path):
    _month_with_empty_windows(tmp_path / "catalog")
    result = run_candle_unit(_plan(tmp_path / "catalog", "donchian_4h_r1", {"entry_bars": 2}))

    decisions = [d for _, d in result.decisions]
    entries = [d for d in decisions if d.intent == Intent.ENTER]
    assert entries
    assert {d.reason for d in entries} <= {"donchian_long", "donchian_short"}
    assert {d.reason for d in decisions if d.intent == Intent.EXIT} <= DONCHIAN_EXITS
    assert all(d.decided_ns % HOUR_NS == 0 for d in decisions)  # each decision on a 1 hour bar close
    assert all(p.quantity == 0 for _, p in result.positions)


def test_the_donchian_4h_r1_study_keeps_the_shared_settings_and_the_grid_on_1_hour_bars():
    reference = load_study(STUDIES / SHARED_FROM)
    study = load_study(STUDIES / "donchian_4h_r1.toml")

    assert _shared(study) == _shared(reference)
    assert (study.section.name, study.section.lab, study.section.seed) == ("donchian_4h_r1", "candle", 42)
    assert isinstance(study.loaded, LoadedCandleRun)
    assert study.loaded.bar_spec == "1-HOUR-LAST"
    config = dict(study.loaded.run.strategy.config)
    assert (config["time_stop_bars"], config["atr_period"]) == (240, 14)
    points = sorted((int(p["entry_bars"]), D(str(p["stop_atr"]))) for _, p in study.points)
    assert points == [(20, D(2)), (20, D(3)), (55, D(2)), (55, D(3))]
