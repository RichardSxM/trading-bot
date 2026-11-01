"""covers: spec 0027.2 AC-13 (``tsmom``'s node module and config checks).

Needs the ``node`` extra.
"""

import uuid
from decimal import Decimal

import pytest

from trading_core.node.strategy import PauseGate
from trading_core.node.tsmom import Tsmom, TsmomConfig
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.tsmom import TsmomRule

D = Decimal


def _config(**overrides):
    values = {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": "PF_SOLUSD.KRAKEN",
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
        "time_stop_bars": 30,
        "atr_period": 20,
        "stop_atr": D(3),
    } | overrides
    return TsmomConfig(**values)  # type: ignore[arg-type]  # each value has its own type


def test_the_defaults_are_daily_bars_long_only_and_the_grids_first_lookback():
    config = _config()
    assert (config.bar_spec, config.bar_interval_ns) == ("1-DAY-LAST", 86_400 * 10**9)
    assert (config.lookback_days, config.long_only) == (20, True)
    assert config.warmup_bars() == 20 + 20 + 1


def test_the_node_subscribes_to_daily_internal_bars_and_builds_the_rule_from_the_config():
    strategy = Tsmom(_config(lookback_days=60), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_SOLUSD.KRAKEN-1-DAY-LAST-INTERNAL"
    assert isinstance(strategy.rule, TsmomRule)
    assert (strategy.rule.lookback_days, strategy.rule.warmup_bars()) == (60, 81)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"long_only": False}, "long_only"),
        ({"bar_spec": "4-HOUR-LAST"}, "bar_spec"),
        ({"lookback_days": 0}, "lookback_days"),
        ({"stop_atr": D(0)}, "stop_atr"),
        ({"time_stop_bars": 0}, "time_stop_bars"),
    ],
)
def test_bad_settings_are_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)
