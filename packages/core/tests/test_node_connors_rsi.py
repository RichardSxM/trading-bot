"""covers: spec 0019.1 (``connors_rsi`` on 15 minute bars: the config accepts it and the node subscribes to it).

Needs the ``node`` extra.
"""

import uuid
from decimal import Decimal

from trading_core.node.connors_rsi import ConnorsRsi, ConnorsRsiConfig
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
    return ConnorsRsiConfig(**values)


def test_connors_rsi_on_15_minute_bars_subscribes_to_the_15_minute_internal_bars():
    config = _config(bar_spec="15-MINUTE-LAST", time_stop_bars=12)
    assert config.bar_interval_ns == 15 * 60 * 10**9

    strategy = ConnorsRsi(config, PauseGate(), EntryBlocks(), lambda item: None)

    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-15-MINUTE-LAST-INTERNAL"
