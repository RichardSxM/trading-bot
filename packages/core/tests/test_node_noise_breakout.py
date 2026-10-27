"""covers: spec 0020 AC-2 and AC-4 (``noise_breakout``'s node module and config checks), 0020.1 *Settings*.

Needs the ``node`` extra.
"""

import uuid
from decimal import Decimal

import pytest

from trading_core.node.noise_breakout import NoiseBreakout, NoiseBreakoutConfig
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.noise_breakout import NoiseBreakoutRule

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
    return NoiseBreakoutConfig(**values)  # type: ignore[arg-type]  # each value has its own type


def test_the_defaults_are_the_spec_settings_on_30_minute_bars_with_an_8_hour_time_stop():
    config = _config()
    assert (config.bar_spec, config.time_stop_bars, config.bar_interval_ns) == ("30-MINUTE-LAST", 16, 1_800 * 10**9)
    assert (config.atr_period, config.atr_floor_mult, config.lookback_days) == (14, D(1), 14)
    assert config.band_mult == D(1)  # the grid's first point


def test_the_node_subscribes_to_30_minute_internal_bars_and_builds_the_rule_from_the_config():
    strategy = NoiseBreakout(_config(band_mult=D("1.5")), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-30-MINUTE-LAST-INTERNAL"
    assert isinstance(strategy.rule, NoiseBreakoutRule)
    assert strategy.rule.days.maxlen == 18  # time_stop_bars + 2


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"band_mult": D(0)}, "band_mult"),
        ({"band_mult": D("-1")}, "band_mult"),
        ({"lookback_days": 0}, "lookback_days"),
        ({"bar_spec": "15-MINUTE-LAST"}, "bar_spec"),
        ({"time_stop_bars": 0}, "time_stop_bars"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)


def test_the_smallest_allowed_settings_are_accepted():
    # The look ahead test's loose config (spec 0020 AC-2) relies on these.
    config = _config(lookback_days=1, band_mult=D("0.1"))
    assert (config.lookback_days, config.band_mult) == (1, D("0.1"))
