"""covers: spec 0019 AC-2 and AC-4 (``ema_pullback``'s node module and config checks), 0019.2 *Settings*.

Needs the ``node`` extra.
"""

import uuid
from decimal import Decimal

import pytest

from trading_core.node.ema_pullback import EmaPullback, EmaPullbackConfig
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks

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
    return EmaPullbackConfig(**values)  # type: ignore[arg-type]  # each value has its own type


def test_the_defaults_are_the_spec_settings_on_15_minute_bars_with_an_8_hour_time_stop():
    config = _config()
    assert (config.bar_spec, config.time_stop_bars) == ("15-MINUTE-LAST", 32)
    assert (config.fast, config.slow, config.rsi_period, config.rsi_low, config.arm_bars) == (8, 21, 7, 30, 6)
    assert (config.target_r, config.rsi_high) == (D("1.5"), 45)  # the grid's first point


def test_the_node_subscribes_to_15_minute_internal_bars():
    strategy = EmaPullback(_config(), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-15-MINUTE-LAST-INTERNAL"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"fast": 21, "slow": 8}, "slow"),
        ({"fast": 1}, "fast"),
        ({"bar_spec": "1-HOUR-LAST"}, "bar_spec"),
        ({"rsi_period": 1}, "rsi_period"),
        ({"rsi_low": 0}, "rsi_low"),
        ({"rsi_low": 45, "rsi_high": 45}, "rsi_low"),
        ({"rsi_high": 55}, "rsi_high"),
        ({"arm_bars": 0}, "arm_bars"),
        ({"target_r": D(0)}, "target_r"),
        ({"time_stop_bars": 0}, "time_stop_bars"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)
