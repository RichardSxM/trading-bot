"""covers: spec 0020 AC-2 and AC-4 (``donchian_4h``'s node module and config checks), 0020.3 *Settings*.

Needs the ``node`` extra.
"""

import uuid
from decimal import Decimal

import pytest

from trading_core.node.donchian_4h import Donchian4h, Donchian4hConfig
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian import DonchianRule

D = Decimal


def _config(**overrides):
    values = {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": "PF_XBTUSD.KRAKEN",
        "use_market_context": False,
        "max_order_notional_usd": D(20000),
        "max_leverage": D(2),
        "max_total_exposure_usd": D(20000),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
        "stale_quote_s": D(300),
    } | overrides
    return Donchian4hConfig(**values)  # type: ignore[arg-type]  # each value has its own type


def test_the_defaults_are_the_spec_settings_on_4_hour_bars_with_a_10_day_time_stop():
    config = _config()
    assert (config.bar_spec, config.time_stop_bars, config.bar_interval_ns) == ("4-HOUR-LAST", 60, 14_400 * 10**9)
    assert (config.atr_period, config.atr_floor_mult) == (14, D(1))
    assert (config.entry_bars, config.stop_atr) == (20, D(2))  # the grid's first point


def test_the_node_subscribes_to_4_hour_internal_bars_and_builds_the_rule_from_the_config():
    strategy = Donchian4h(_config(entry_bars=55, stop_atr=D(3)), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-4-HOUR-LAST-INTERNAL"
    assert isinstance(strategy.rule, DonchianRule)
    assert (strategy.rule.entry_bars, strategy.rule.exit_bars) == (55, 27)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"entry_bars": 1}, "entry_bars"),
        ({"stop_atr": D("0.5")}, "stop_atr"),
        ({"bar_spec": "1-HOUR-LAST"}, "bar_spec"),
        ({"time_stop_bars": 0}, "time_stop_bars"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)


def test_the_smallest_allowed_settings_are_accepted():
    # The look ahead test's loose config (spec 0020 AC-2) relies on entry_bars 2.
    config = _config(entry_bars=2, stop_atr=D(1))
    assert (config.entry_bars, config.stop_atr) == (2, D(1))
