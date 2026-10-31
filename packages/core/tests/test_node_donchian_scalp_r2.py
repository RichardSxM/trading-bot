"""covers: spec 0026 AC-13 (the volatility floor), AC-14 (r2's config, its rule, the spread check after the floor,
the seed join, decision inputs).

Runs ``DonchianScalpR2`` in ``test_node_donchian_scalp_r1``'s BacktestEngine harness (5 minute bars, entry 2, EMA 2,
ATR 2). Its synthetic prices give an ATR near 6 bps of the price, so a test that needs entries turns the floor
off or lowers it; the floor's boundary is tested on ``_pre_entry`` directly. Needs the ``node`` extra.
"""

import logging
import uuid
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.model.enums import OrderType
from test_node_donchian_scalp_r1 import (
    FIVE_MIN_NS,
    START,
    Script,
    _codes,
    _decisions,
    _fields,
    _run,
    _seed,
    flat,
)

from trading_core.control.pause import PauseState
from trading_core.node.donchian_scalp_r2 import DonchianScalpR2, DonchianScalpR2Config
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import ControlSource, Intent, OrderSide, RiskVerdict
from trading_core.recording.rows import RiskEventRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian_midline import DonchianMidlineRule

D = Decimal


def _config(**overrides):
    values: dict[str, Any] = _fields(entry_bars=2, stop_atr=D(1), ema_period=2) | overrides
    return DonchianScalpR2Config(**values)


class _Scripted(DonchianScalpR2):
    """r2 with a scripted rule in place of its own, so the filters are tested alone."""

    def __init__(self, config, gate, blocks, record, rule):
        super().__init__(config, gate, blocks, record)
        self.rule = rule


def _scripted(entries, **overrides):
    return lambda gate, blocks, record: _Scripted(_config(**overrides), gate, blocks, record, Script(entries))


class _Counting(DonchianScalpR2):
    """r2 that notes the close time of every bar its rule is fed."""

    def __init__(self, config, gate, blocks, record, seed=()):
        super().__init__(config, gate, blocks, record, seed)
        self.fed: list[int] = []
        update = self.rule.update

        def counted(bar, atr):
            self.fed.append(bar.ts_ns)
            update(bar, atr)

        self.rule.update = counted  # type: ignore[method-assign]


# Config (AC-14) -----------------------------------------------------------------------------


def test_the_defaults_are_the_demo_settings_with_the_spread_check_off():
    config = DonchianScalpR2Config(**{k: v for k, v in _fields().items() if k != "time_stop_bars"})
    assert (config.bar_spec, config.entry_bars, config.stop_atr, config.time_stop_bars, config.ema_period) == (
        "5-MINUTE-LAST",
        20,
        D(3),
        60,
        200,
    )
    assert (config.entry_offset_atr, config.entry_wait_bars, config.exit_wait_bars) == (D("0.5"), 3, 3)
    assert (config.atr_min_bps, config.spread_max_atr, config.atr_floor_mult) == (D(10), None, D(1))


def test_the_node_builds_the_midline_rule_from_the_config():
    strategy = DonchianScalpR2(_config(entry_bars=20, ema_period=200), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-5-MINUTE-LAST-INTERNAL"
    assert isinstance(strategy.rule, DonchianMidlineRule)
    assert (strategy.rule.inner.entry_bars, strategy.rule.ema.n) == (20, 200)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"entry_wait_bars": 0}, "entry_wait_bars"),
        ({"exit_wait_bars": 0}, "exit_wait_bars"),
        ({"entry_offset_atr": D("-0.1")}, "entry_offset_atr"),
        ({"atr_min_bps": D(-1)}, "atr_min_bps"),
        ({"bar_spec": "1-MINUTE-LAST"}, "bar_spec"),
        ({"spread_max_atr": D(0)}, "spread_max_atr"),
        ({"ema_period": 1}, "ema_period"),
        ({"entry_bars": 1}, "entry_bars"),
        ({"stop_atr": D("0.5")}, "stop_atr"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)


def test_a_zero_offset_and_a_zero_floor_are_allowed():
    config = _config(entry_offset_atr=D(0), atr_min_bps=D(0))
    assert (config.entry_offset_atr, config.atr_min_bps) == (D(0), D(0))


# The volatility floor (AC-13) ---------------------------------------------------------------


def test_a_low_atr_records_vol_low_and_sends_no_order(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, rows = _run(_scripted({5}))  # the default floor of 10 bps is above this data's ATR

    [refused] = _decisions(rows)
    assert (refused.intent, refused.side, refused.reason) == (Intent.ENTER, OrderSide.BUY, "script_long")
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "vol_low")
    inputs = refused.inputs
    assert {"bar_close", "atr", "n", "structural_stop", "atr_bps", "atr_min_bps"} == set(inputs)
    atr_bps = D(str(inputs["atr"])) / D(str(inputs["bar_close"])) * 10_000
    assert D(str(inputs["atr_bps"])) == atr_bps < 10
    assert D(str(inputs["atr_min_bps"])) == 10
    assert engine.cache.orders() == []
    assert ("entry_skipped_vol", None) in _codes(caplog)
    assert not [r for r in rows if isinstance(r, RiskEventRow)]


def test_with_the_floor_off_the_entry_goes_out_as_a_maker_limit():
    engine, _, rows = _run(_scripted({5}, atr_min_bps=None))

    [enter] = _decisions(rows)
    assert (enter.risk_verdict, enter.risk_reason) == (RiskVerdict.APPROVED, None)
    [entry] = engine.cache.orders()
    assert (entry.order_type, entry.is_post_only) == (OrderType.LIMIT, True)


def test_a_paused_gate_with_a_low_atr_records_the_pause_not_vol_low():
    gate = PauseGate()
    gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))

    _, _, rows = _run(_scripted({5}), gate=gate)

    [refused] = _decisions(rows)
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "paused")


@cache
def _after_run():
    _, strategy, _ = _run(_scripted((), spread_max_atr=D("0.1")))
    return strategy


@pytest.mark.parametrize(("atr", "passes"), [(D("85.0"), True), (D("84.915"), False), (D("100"), True)])
def test_the_floor_passes_an_atr_at_ten_bps_and_refuses_one_just_below(atr, passes):
    # The last quote is 1 either side of the mid (a spread of 2), well inside 0.1 ATR at these ATRs.
    strategy = _after_run()
    written: list[WriteItem] = []
    strategy._record = written.append
    bar = BarInput(0, D(85000), D(85001), D(84999), D(85000), D(1))

    result = strategy._pre_entry(bar, atr, EnterSignal(Side.LONG, D(84000), "script_long"))

    reasons = [d.risk_reason for d in _decisions([w.row for w in written])]
    assert (result, reasons) == ((True, []) if passes else (False, ["vol_low"]))


def test_the_floor_runs_before_the_spread_check():
    strategy = _after_run()
    written: list[WriteItem] = []
    strategy._record = written.append
    bar = BarInput(0, D(85000), D(85001), D(84999), D(85000), D(1))

    # ATR 10: 1.2 bps (below the floor) and 0.1 ATR is 1, under the spread of 2 (wide too).
    assert strategy._pre_entry(bar, D(10), EnterSignal(Side.LONG, D(84000), "script_long")) is False
    assert [d.risk_reason for d in _decisions([w.row for w in written])] == ["vol_low"]


# The spread check (AC-14) -------------------------------------------------------------------


def test_a_wide_spread_records_spread_wide_once_the_floor_passes():
    engine, _, rows = _run(_scripted({5}, atr_min_bps=None, spread_max_atr=D("0.01")))

    [refused] = _decisions(rows)
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "spread_wide")
    assert {"bid", "ask", "spread", "spread_limit", "mid", "quote_age_ms", "structural_stop"} <= set(refused.inputs)
    assert engine.cache.orders() == []


# The warmup seed (AC-14) --------------------------------------------------------------------


@cache
def _live_closes():
    _, strategy, _ = _run(lambda g, b, r: _Counting(_config(), g, b, r), prices=flat)
    return tuple(strategy.fed)


def _with_seed(seed):
    return lambda g, b, r: _Counting(_config(), g, b, r, seed)


@pytest.mark.parametrize("gap", [FIVE_MIN_NS, 2 * FIVE_MIN_NS])
def test_a_seed_up_to_two_intervals_old_is_fed_and_the_first_live_bar_ignored(gap, caplog):
    live = _live_closes()
    seed = _seed(live[0] - gap)

    with caplog.at_level(logging.INFO):
        _, strategy, rows = _run(_with_seed(seed), prices=flat)

    assert strategy.fed == [b.ts_ns for b in seed] + list(live[1:])
    assert strategy.rule.ready()
    assert ("warmup_seed_fed", None) in _codes(caplog)
    assert _decisions(rows) == []


def test_a_seed_three_intervals_old_is_dropped_as_stale(caplog):
    live = _live_closes()

    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(_with_seed(_seed(live[0] - 3 * FIVE_MIN_NS)), prices=flat)

    assert strategy.fed == list(live[1:])
    stale = [r for r in caplog.records if getattr(r, "fields", {}).get("reason") == "seed_stale"]
    assert [(r.levelname, r.fields["code"]) for r in stale] == [("WARNING", "warmup_seed_failed")]


def test_a_live_bar_at_or_before_the_seed_s_last_close_is_ignored():
    live = _live_closes()
    seed = _seed(live[1])

    _, strategy, _ = _run(_with_seed(seed), prices=flat)

    assert strategy.fed == [b.ts_ns for b in seed] + list(live[2:])


def test_an_entry_signal_on_the_last_seed_candle_never_trades():
    live = _live_closes()
    first = live[0] - 6 * FIVE_MIN_NS
    seed = [BarInput(first + i * FIVE_MIN_NS, D(85000), D(86000), D(84000), D(85000), D(1)) for i in range(5)]
    seed.append(BarInput(live[0] - FIVE_MIN_NS, D(85000), D(86600), D(84000), D(86500), D(1)))

    engine, strategy, rows = _run(_with_seed(seed), prices=flat)

    assert strategy.rule.ready()
    assert _decisions(rows) == []
    assert engine.cache.orders() == []


# Decision inputs (AC-14) --------------------------------------------------------------------


def test_a_decision_carries_the_rule_s_four_values():
    _, _, rows = _run(lambda g, b, r: DonchianScalpR2(_config(atr_min_bps=None), g, b, r))

    enter = next(d for d in _decisions(rows) if d.intent == Intent.ENTER)
    assert {"ema", "midline", "entry_high", "entry_low", "bar_close", "atr", "limit_px"} <= set(enter.inputs)
    assert enter.inputs["ema"] is not None
    assert enter.inputs["midline"] is not None
