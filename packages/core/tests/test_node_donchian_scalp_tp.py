"""covers: ``donchian_scalp_tp``: the live trailing stop and hard stop, the order cap's size, and the warmup seed.

Runs the node with a scripted rule in a BacktestEngine on the synthetic data of ``test_node_bar_rule``.
Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_bar_rule import INSTRUMENT, INSTRUMENT_ID, START, Script, _by_type, _data, _decisions, _ns, rising

from trading_core.node.bar_rule import Phase
from trading_core.node.donchian_scalp_tp import DonchianScalpTp, DonchianScalpTpConfig
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput, Side
from trading_core.strategy.blocks import EntryBlocks

D = Decimal


class _Scripted(DonchianScalpTp):
    def __init__(self, config, gate, blocks, record, rule, seed=()):
        super().__init__(config, gate, blocks, record, seed)
        self.rule = rule


def _config(**overrides):
    values: dict[str, Any] = {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "1-MINUTE-LAST",
        "atr_period": 2,
        "time_stop_bars": 30,
        "use_market_context": False,
        "max_order_notional_usd": D(3000),
        "max_leverage": D(10),
        "max_total_exposure_usd": D(10000),
        "risk_per_trade": D("0.5"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(5),
        "liq_stop_multiple": D(1),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
        "stale_quote_s": D(60),
    } | overrides
    return DonchianScalpTpConfig(**values)


def _run(rule, prices=rising, minutes=20, seed=(), **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, [Money(10000, USD)], base_currency=USD)
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_data(prices, minutes, 0))
    records: list[WriteItem] = []
    strategy = _Scripted(_config(**overrides), PauseGate(), EntryBlocks(), records.append, rule, seed)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _wobble(t):
    """Near 85000, moving enough that the ATR is not 0."""
    return 85000 + int(t) % 2 * 20


def _jump(sign):
    """Near 85000 for 8 minutes, then 1% away in the direction of ``sign``."""

    def prices(t):
        return _wobble(t) if t < 8 else 85000 + sign * 850

    return prices


def _out_and_back(sign):
    """Near 85000 for 8 minutes, 1% away in the direction of ``sign`` until minute 12, then back near 85000."""

    def prices(t):
        return 85000 + sign * 850 if 8 <= t < 12 else _wobble(t)

    return prices


def _bar(minutes_after_start, close=85000):
    px = D(close)
    return BarInput(_ns(START + timedelta(minutes=minutes_after_start)), px, px + 10, px - 10, px, D(1))


def _closed_by(engine, strategy, rows, reason, at_minute):
    enter, exit_ = _decisions(rows)
    assert (enter.intent, exit_.intent, exit_.reason) == (Intent.ENTER, Intent.EXIT, reason)
    assert (exit_.inputs["trail_arm_usd"], exit_.inputs["trail_usd"], exit_.inputs["stop_loss_usd"]) == (
        "1.5",
        "0.5",
        "3",
    )
    assert exit_.decided_ns == _ns(START + timedelta(minutes=at_minute))
    assert strategy.phase == Phase.FLAT
    assert engine.cache.positions_open() == []
    stops = _by_type(engine, OrderType.STOP_MARKET)
    assert len(stops) == 1
    assert stops[0].is_canceled
    return exit_


@pytest.mark.parametrize(("side", "sign"), [(Side.LONG, 1), (Side.SHORT, -1)])
def test_a_gain_past_the_arm_level_that_falls_back_exits_on_the_trail(side, sign):
    engine, strategy, rows = _run(Script(entries={5: (side, 2000)}), prices=_out_and_back(sign))

    exit_ = _closed_by(engine, strategy, rows, "trailing_stop", 12)
    assert D(exit_.inputs["best_pnl"]) >= D("1.5")
    assert D(exit_.inputs["open_pnl"]) <= D(exit_.inputs["best_pnl"]) - D("0.5")


@pytest.mark.parametrize(("side", "sign"), [(Side.LONG, 1), (Side.SHORT, -1)])
def test_a_gain_that_holds_keeps_the_position_open(side, sign):
    engine, strategy, rows = _run(Script(entries={5: (side, 2000)}), prices=_jump(sign))

    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER]
    assert strategy.phase == Phase.PROTECTED
    assert len(engine.cache.positions_open()) == 1


@pytest.mark.parametrize(("side", "sign"), [(Side.LONG, -1), (Side.SHORT, 1)])
def test_a_loss_past_the_hard_stop_closes_at_market_on_the_first_quote_past_it(side, sign):
    engine, strategy, rows = _run(Script(entries={5: (side, 2000)}), prices=_jump(sign))

    _closed_by(engine, strategy, rows, "stop_loss", 8)


def test_a_move_inside_both_exits_leaves_the_position_open_and_its_stop_resting():
    engine, strategy, rows = _run(Script(entries={5: (Side.LONG, 500)}), prices=_wobble)

    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER]
    assert strategy.phase == Phase.PROTECTED
    assert len(engine.cache.positions_open()) == 1
    assert _by_type(engine, OrderType.STOP_MARKET)[0].is_open


def test_the_order_cap_sets_the_size_at_the_demo_s_risk_per_trade():
    engine, _, rows = _run(Script(entries={5: (Side.LONG, 500)}), prices=_wobble)

    enter = _decisions(rows)[0]
    assert enter.risk_reason is None
    entry = _by_type(engine, OrderType.MARKET)[0]
    notional = entry.quantity.as_decimal() * D(85000)
    assert D(2990) < notional <= D(3000)


def test_the_seed_is_fed_at_the_first_live_bar_and_that_partial_bar_is_dropped():
    _, _, _ = _run(no_seed := Script(), minutes=5)
    seed = [_bar(-5 + i) for i in range(5)]

    _, _, rows = _run(seeded := Script(), minutes=5, seed=seed)

    assert seeded.n == no_seed.n - 1 + len(seed)
    assert _decisions(rows) == []


def test_a_signal_on_the_seed_s_last_bar_never_trades():
    seed = [_bar(-5 + i) for i in range(5)]

    _, _, rows = _run(rule := Script(entries={5: (Side.LONG, 500)}), minutes=4, seed=seed)

    assert 5 in rule.consumed_at
    assert _decisions(rows) == []


def test_a_seed_too_old_for_the_first_live_bar_is_dropped():
    _, _, _ = _run(no_seed := Script(), minutes=5)
    old = [_bar(-500 + i) for i in range(5)]

    _, _, _ = _run(rule := Script(), minutes=5, seed=old)

    assert rule.n == no_seed.n - 1


@pytest.mark.parametrize("field", ["trail_arm_usd", "trail_usd", "stop_loss_usd"])
@pytest.mark.parametrize("value", ["0", "-2"])
def test_the_config_refuses_a_dollar_exit_that_is_not_above_zero(field, value):
    with pytest.raises(ValueError, match=field):
        _config(**{field: D(value)})
