"""covers: spec 0023 AC-15 (each strategy base ignores an entry signal from data older than the last check) and
AC-16 (each strategy base's ``resume_position``: it keeps the stop, rebuilds its exit timers from the position's
open time and exits when it would have without the restart, or at once when that time has passed; ``BuyAndHold``
keeps holding; ``RandomTaker`` uses its timed exit; ``ReconcileExecutor`` closes).

A restart is simulated inside each base's own BacktestEngine harness: at ``forget_min`` the strategy loses its
round trip state (as a new process would), and at ``resume_min`` it is handed the open position and its resting
stop, as the reconciliation check does. Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderStatus, OrderType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price
from test_node_bar_rule import INSTRUMENT, INSTRUMENT_ID, START, Script, _at, _data, _ns, rising
from test_node_bar_rule import _decisions as _bar_decisions
from test_node_bar_rule import _run as _bar_run
from test_node_placeholder import Prices
from test_node_placeholder import _decisions as _placeholder_decisions
from test_node_placeholder import _run as _placeholder_run
from test_node_random_taker import RISING
from test_node_random_taker import _run as _taker_run
from test_node_scalp_base import FLAT_MARKET, LONG, MAKER_EXIT, _Scripted, _targets
from test_node_scalp_base import _decisions as _scalp_decisions
from test_node_scalp_base import _orders as _scalp_orders
from test_node_scalp_base import _run as _scalp_run

from trading_core.node.bar_rule import BarRuleStrategy
from trading_core.node.bar_rule import Phase as BarPhase
from trading_core.node.buy_and_hold import BuyAndHold, BuyAndHoldConfig
from trading_core.node.buy_and_hold import Phase as HoldPhase
from trading_core.node.macd_baseline import MacdBaseline, MacdBaselineConfig
from trading_core.node.macd_baseline import Phase as MacdPhase
from trading_core.node.placeholder import PlaceholderTimed
from trading_core.node.random_taker import RandomTaker
from trading_core.node.reconcile import resting_stop
from trading_core.node.reconcile_executor import ReconcileExecutor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, OrderSide
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import Side
from trading_core.strategy.blocks import BlockReason, EntryBlocks

D = Decimal
MINUTE_NS = 60_000_000_000


if TYPE_CHECKING:
    from trading_core.node.strategy import KfsStrategy as _Mixin
else:
    _Mixin = object


def _min(minutes):
    return _ns(START + timedelta(minutes=minutes))


class _Restart(_Mixin):
    """Loses its round trip state at ``forget_min`` (a restart) and is handed the open position at ``resume_min``.

    ``resumed`` is what it was handed: the position, its resting stop, and the state right after the resume.
    """

    forget_min = 0.0
    resume_min = 0.0

    def on_start(self):
        super().on_start()
        cls = type(self)
        self.clock.set_time_alert_ns("test_forget", _min(cls.forget_min), self._forget)
        self.clock.set_time_alert_ns("test_resume", _min(cls.resume_min), self._resume)

    def _forget(self, _event):
        forget = getattr(self, "_back_to_flat", None) or self._reset
        forget()

    def _resume(self, _event):
        (position,) = self.cache.positions_open()
        stop = resting_stop(self.cache, position)
        self.resume_position(position, stop)
        self.resumed = (position, stop, self.phase)


class _FreshAfter(_Mixin):
    """A start or reconnect check finished at ``after_min``: older signals are stale."""

    after_min = 0.0

    def on_start(self):
        super().on_start()
        self.signals_after(_min(type(self).after_min))


# BarRuleStrategy ---------------------------------------------------------------------------------


class _BarRestart(_Restart, BarRuleStrategy):
    pass


def test_bar_rule_resumed_keeps_its_stop_and_time_stops_at_the_bar_it_would_have():
    _BarRestart.forget_min, _BarRestart.resume_min = 5.5, 5.6
    engine, strategy, rows = _bar_run(Script(entries={5: (Side.LONG, 30)}), strategy_cls=_BarRestart, time_stop_bars=3)

    assert [(d.intent, d.reason, d.decided_ns) for d in _bar_decisions(rows)] == [
        (Intent.ENTER, "script_long", _at(5)),
        (Intent.EXIT, "time_stop", _at(8)),  # as without the restart
    ]
    (stop,) = [o for o in engine.cache.orders() if o.order_type == OrderType.STOP_MARKET]
    _, resumed_stop, phase = strategy.resumed
    assert resumed_stop.client_order_id == stop.client_order_id  # kept, never placed again
    assert phase == BarPhase.PROTECTED
    assert stop.status == OrderStatus.CANCELED  # the exit cancelled it
    assert engine.cache.positions_open() == []


def test_bar_rule_past_its_time_stop_exits_at_once_on_resume():
    _BarRestart.forget_min, _BarRestart.resume_min = 5.5, 8.5
    engine, _, rows = _bar_run(Script(entries={5: (Side.LONG, 30)}), strategy_cls=_BarRestart, time_stop_bars=3)

    exit_ = _bar_decisions(rows)[-1]
    assert (exit_.intent, exit_.reason, exit_.decided_ns) == (Intent.EXIT, "time_stop", _min(8.5))
    assert exit_.inputs["resumed"] is True
    assert engine.cache.positions_open() == []


class _BarFresh(_FreshAfter, BarRuleStrategy):
    after_min = 6.0  # bar 7 closes at 12:06


def test_bar_rule_ignores_a_signal_from_a_bar_before_the_check():
    rule = Script(entries={6: (Side.LONG, 30), 8: (Side.LONG, 30)})
    _, _, rows = _bar_run(rule, strategy_cls=_BarFresh)

    assert [d.decided_ns for d in _bar_decisions(rows) if d.intent == Intent.ENTER] == [_at(8)]
    assert rule.consumed_at == [6, 8]  # the stale one is consumed, never kept for later


# ScalpStrategy -----------------------------------------------------------------------------------


class _ScalpRestart(_Restart, _Scripted):
    pass


def _scalp_first_fill(engine):
    entry = next(o for o in _scalp_orders(engine, OrderType.MARKET) if not o.is_reduce_only)
    return entry.ts_last


def test_scalp_resumed_time_stops_time_stop_min_after_the_position_opened():
    _ScalpRestart.forget_min, _ScalpRestart.resume_min = 2.0, 2.1
    engine, strategy, rows = _scalp_run(
        lambda t: FLAT_MARKET, 9, LONG, strategy_cls=_ScalpRestart, entry_mode="taker", time_stop_min=3
    )

    (exit_decision,) = _scalp_decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "time_stop"
    assert exit_decision.decided_ns == _scalp_first_fill(engine) + 3 * MINUTE_NS
    (stop,) = _scalp_orders(engine, OrderType.STOP_MARKET)
    assert strategy.resumed[1].client_order_id == stop.client_order_id
    assert engine.cache.positions_open() == []


def test_scalp_past_its_time_stop_exits_at_once_on_resume():
    _ScalpRestart.forget_min, _ScalpRestart.resume_min = 2.0, 4.5
    engine, _, rows = _scalp_run(
        lambda t: FLAT_MARKET, 9, LONG, strategy_cls=_ScalpRestart, entry_mode="taker", time_stop_min=3
    )

    (exit_decision,) = _scalp_decisions(rows, Intent.EXIT)
    assert (exit_decision.reason, exit_decision.decided_ns) == ("time_stop", _min(4.5))
    assert engine.cache.positions_open() == []


def test_scalp_keeps_a_resting_maker_target_and_exits_on_its_fill():
    _ScalpRestart.forget_min, _ScalpRestart.resume_min = 1.2, 1.3
    engine, _strategy, rows = _scalp_run(
        lambda t: 85_000 + int(t * 60),
        6,
        {1: (OrderSide.BUY, -200, 100)},
        strategy_cls=_ScalpRestart,
        entry_mode="taker",
        **MAKER_EXIT,
    )

    (target,) = _targets(engine)  # the one placed before the restart, never a second one
    assert target.status == OrderStatus.FILLED
    (exit_decision,) = _scalp_decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "target"
    assert engine.cache.positions_open() == []


class _ScalpFresh(_FreshAfter, _Scripted):
    after_min = 1.5  # bar 1 closes at 12:00, bar 3 at 12:02


def test_scalp_ignores_a_signal_from_a_bar_before_the_check():
    script = {1: (OrderSide.BUY, -200, 300), 3: (OrderSide.BUY, -200, 300)}
    _, _, rows = _scalp_run(lambda t: FLAT_MARKET, 5, script, strategy_cls=_ScalpFresh, entry_mode="taker")

    (enter,) = _scalp_decisions(rows, Intent.ENTER)
    assert enter.decided_ns == _min(2)


# PlaceholderTimed --------------------------------------------------------------------------------


class _PlaceholderRestart(_Restart, PlaceholderTimed):
    pass


def test_placeholder_resumed_exits_on_time_hold_min_after_the_position_opened():
    _PlaceholderRestart.forget_min, _PlaceholderRestart.resume_min = 17, 17.1
    engine, strategy, rows = _placeholder_run(
        Prices(29, lambda t: 85000 + int(t * 10)), strategy_cls=_PlaceholderRestart
    )

    _enter, exit_ = _placeholder_decisions(rows)
    position, stop, _ = strategy.resumed
    assert (exit_.intent, exit_.reason) == (Intent.EXIT, "time")
    assert exit_.decided_ns == position.ts_opened + 10 * MINUTE_NS
    assert stop is not None
    assert [o.order_type for o in engine.cache.orders()] == [OrderType.MARKET, OrderType.STOP_MARKET, OrderType.MARKET]
    assert engine.cache.positions_open() == []


def test_placeholder_past_its_hold_exits_at_once_on_resume():
    _PlaceholderRestart.forget_min, _PlaceholderRestart.resume_min = 17, 26
    engine, _, rows = _placeholder_run(Prices(29, lambda t: 85000 + int(t * 10)), strategy_cls=_PlaceholderRestart)

    _, exit_ = _placeholder_decisions(rows)
    assert (exit_.reason, exit_.decided_ns) == ("time", _min(26))
    assert engine.cache.positions_open() == []


class _PlaceholderFresh(_FreshAfter, PlaceholderTimed):
    after_min = 15.5  # after the 12:15:02 decision time


def test_placeholder_skips_a_boundary_scheduled_before_the_check():
    _, _, rows = _placeholder_run(Prices(29, lambda t: 85000 + int(t * 10)), strategy_cls=_PlaceholderFresh)

    assert _placeholder_decisions(rows) == []


# RandomTaker -------------------------------------------------------------------------------------


class _TakerRestart(_Restart, RandomTaker):
    pass


def _taker_decisions(rows, intent):
    from trading_core.recording.rows import DecisionRow

    return [r for r in rows if isinstance(r, DecisionRow) and r.intent == intent]


def test_random_taker_resumed_exits_hold_s_after_the_position_opened():
    _TakerRestart.forget_min, _TakerRestart.resume_min = 1.2, 1.3
    _, strategy, rows = _taker_run(RISING, strategy_cls=_TakerRestart)

    position, stop, _ = strategy.resumed
    assert stop is None  # it never places one
    first_exit = _taker_decisions(rows, Intent.EXIT)[0]
    assert first_exit.decided_ns == position.ts_opened + 30_000_000_000


def test_random_taker_past_its_hold_exits_at_once_on_resume():
    _TakerRestart.forget_min, _TakerRestart.resume_min = 1.2, 1.75
    _, _, rows = _taker_run(RISING, strategy_cls=_TakerRestart)

    first_exit = _taker_decisions(rows, Intent.EXIT)[0]
    assert first_exit.decided_ns == _min(1.75)


class _TakerFresh(_FreshAfter, RandomTaker):
    after_min = 1.5  # after the 12:01 boundary


def test_random_taker_skips_a_boundary_scheduled_before_the_check():
    _, _, rows = _taker_run(RISING, strategy_cls=_TakerFresh)

    assert [d.decided_ns for d in _taker_decisions(rows, Intent.ENTER)] == [_min(2)]


# MacdBaseline and BuyAndHold ---------------------------------------------------------------------


def _risk_values() -> dict[str, Any]:
    return {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "1-MINUTE-LAST",
        "use_market_context": False,
        "max_order_notional_usd": D(50000),
        "max_leverage": D(5),
        "max_total_exposure_usd": D(50000),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
        "stale_quote_s": D(60),
        "notional_usd": D(500),
    }


def _engine_run(strategy_cls, config, minutes=10):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(10000, USD)], base_currency=USD
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_data(rising, minutes, 0))
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    strategy = strategy_cls(config, PauseGate(), blocks, records.append)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, blocks, [item.row for item in records]


DECISION = uuid.UUID(int=42)


class _MacdAdopts(_Restart, MacdBaseline):
    """Opens a long and rests its stop by hand (the MACD rule needs a long warm up), then is resumed."""

    with_stop = True
    forget_min, resume_min = 3.0, 3.0

    def on_start(self):
        MacdBaseline.on_start(self)
        self.clock.set_time_alert_ns("test_open", _min(1), self._open)
        self.clock.set_time_alert_ns("test_resume", _min(3), self._resume)

    def _open(self, _event):
        entry = self.order_factory.market(
            INSTRUMENT_ID, NautilusOrderSide.BUY, INSTRUMENT.make_qty(D("0.0050")), tags=[f"{DECISION_TAG}{DECISION}"]
        )
        self.submit_order(entry)
        if type(self).with_stop:
            stop = self.order_factory.stop_market(
                INSTRUMENT_ID,
                NautilusOrderSide.SELL,
                INSTRUMENT.make_qty(D("0.0050")),
                trigger_price=Price(80000, 0),
                reduce_only=True,
            )
            self.submit_order(stop)


class _MacdNoStop(_MacdAdopts):
    with_stop = False


def test_macd_resumed_is_protected_by_the_stop_it_was_handed():
    _, strategy, _, _ = _engine_run(_MacdAdopts, MacdBaselineConfig(**_risk_values()))

    _, stop, phase = strategy.resumed
    assert phase == MacdPhase.PROTECTED
    assert strategy._stop_id == stop.client_order_id
    assert strategy.entry_decision == DECISION


def test_macd_resumed_without_a_stop_blocks_entries_and_closes_the_position():
    engine, _, blocks, _ = _engine_run(_MacdNoStop, MacdBaselineConfig(**_risk_values()))

    assert BlockReason.STOP_NOT_ACCEPTED in blocks.active()
    assert engine.cache.positions_open() == []


class _Funded(BuyAndHold):
    """A funding rate is known (the market context is off here), so its open ended hold can be judged."""

    def _funding_rate(self):
        return D(0)


class _HoldRestart(_Restart, _Funded):
    forget_min, resume_min = 3.0, 3.1


def test_buy_and_hold_resumed_keeps_holding_and_never_buys_again():
    engine, strategy, _, _ = _engine_run(_HoldRestart, BuyAndHoldConfig(**_risk_values()))

    assert strategy.resumed[2] == HoldPhase.HOLDING
    assert strategy.phase == HoldPhase.HOLDING
    assert [o.side for o in engine.cache.orders()] == [NautilusOrderSide.BUY]  # one entry, no exit
    assert len(engine.cache.positions_open()) == 1


class _HoldFresh(_FreshAfter, _Funded):
    after_min = 1.5


def test_buy_and_hold_ignores_bars_before_the_check():
    _, _, _, rows = _engine_run(_HoldFresh, BuyAndHoldConfig(**_risk_values()))

    from trading_core.recording.rows import DecisionRow

    (enter,) = [r for r in rows if isinstance(r, DecisionRow)]
    assert enter.decided_ns == _at(3)  # bar 2 closes at 12:01, before the check; bar 3 at 12:02


# ReconcileExecutor -------------------------------------------------------------------------------


def test_the_executor_closes_a_position_of_its_own_on_resume():
    from test_node_reconcile import FakeExchange
    from test_reconcile_executor import FLAT, _executor

    executor, _ = _executor(FakeExchange(FLAT))
    closed: list[object] = []
    executor.close = closed.append

    class _Held:
        id = "PF_XBTUSD.KRAKEN-ReconcileExecutor-000"

    ReconcileExecutor.resume_position(executor, _Held(), None)

    assert closed == ["PF_XBTUSD.KRAKEN-ReconcileExecutor-000"]
