"""covers: spec 0018 AC-1 (bar order, one action per close, the stuck exit, the kill switch), AC-2 (the ATR floor,
a bad signal, sizing from the stop, no quote, consumed signals), AC-3 (the stop at the decided price, stop through,
a rejected stop), AC-4 (the time stop, the rule's exit, the reset on an outside close), AC-5 (decision inputs),
AC-8 (the base config's checks).

Runs ``BarRuleStrategy`` with a scripted rule in a BacktestEngine on synthetic trades and quotes for a PF_XBTUSD
shaped instrument (tick 1, size step 0.0001), on 1 minute bars. Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy, Phase, bar_interval_ns
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, OrderSide, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.mappers import HALT_TAG, ORIGIN_TAG
from trading_core.recording.rows import DecisionRow, RiskEventRow
from trading_core.recording.spool import WriteItem
from trading_core.risk.funding import funding_periods
from trading_core.strategy.bar_rule import EnterSignal, Side
from trading_core.strategy.blocks import BlockReason, EntryBlocks

D = Decimal
START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
MINUTE_NS = 60_000_000_000
INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
INSTRUMENT = CryptoPerpetual(
    instrument_id=INSTRUMENT_ID,
    raw_symbol=Symbol("PF_XBTUSD"),
    base_currency=BTC,
    quote_currency=USD,
    settlement_currency=USD,
    is_inverse=False,
    price_precision=0,
    size_precision=4,
    price_increment=Price.from_str("1"),
    size_increment=Quantity.from_str("0.0001"),
    margin_init=Decimal("0.02"),
    margin_maint=Decimal("0.01"),
    maker_fee=Decimal("0.0002"),
    taker_fee=Decimal("0.0005"),
    ts_event=0,
    ts_init=0,
)
HALT_ID = uuid.UUID(int=7)


def _ns(at):
    return int(at.timestamp() * 1_000_000_000)


def rising(t):
    return 85000 + int(t * 10)


def _data(prices, minutes, quotes_from_min, every_s=10):
    """A trade and a quote (1 either side of the mid) every ``every_s`` seconds; ``prices(t)`` at t minutes."""
    data, at = [], START
    for step in range(int(minutes * 60 / every_s)):
        t = step * every_s / 60
        mid, ts = prices(t), _ns(at)
        data.append(
            TradeTick(
                INSTRUMENT_ID,
                Price(mid, 0),
                Quantity.from_str("0.0100"),
                AggressorSide.BUYER,
                TradeId(f"T{step}"),
                ts,
                ts,
            )
        )
        if t >= quotes_from_min:
            data.append(
                QuoteTick(
                    instrument_id=INSTRUMENT_ID,
                    bid_price=Price(mid - 1, 0),
                    ask_price=Price(mid + 1, 0),
                    bid_size=Quantity.from_str("5.0000"),
                    ask_size=Quantity.from_str("5.0000"),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
        at += timedelta(seconds=every_s)
    return data


class Script:
    """A scripted ``BarRule``: ready from bar ``warm``; at bar ``n`` in ``entries`` it signals ``(side, offset)``,
    the structural stop ``offset`` below the close for a long (above for a short); bars in ``exits`` exit."""

    def __init__(self, entries=None, exits=(), warm=3):
        self.entries = entries or {}
        self.exits = set(exits)
        self.warm = warm
        self.n = 0
        self.close = None
        self.atrs = []
        self.used = set()
        self.consumed_at = []
        self.flats = 0

    def update(self, bar, atr):
        self.n += 1
        self.close = bar.close
        self.atrs.append(atr)

    def ready(self):
        return self.n >= self.warm

    def entry(self):
        planned = self.entries.get(self.n)
        if planned is None or self.n in self.used:
            return None
        side, offset = planned
        assert self.close is not None
        stop = self.close - D(offset) if side == Side.LONG else self.close + D(offset)
        return EnterSignal(side, stop, f"script_{side}")

    def consumed(self):
        self.used.add(self.n)
        self.consumed_at.append(self.n)

    def exit(self, trade):
        self.last_trade = trade
        return "target" if self.n in self.exits else None

    def on_flat(self):
        self.flats += 1

    def values(self):
        return {"n": self.n}


def _config(**overrides):
    values: dict[str, Any] = {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "1-MINUTE-LAST",
        "atr_period": 2,
        "time_stop_bars": 10,
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
    } | overrides
    return BarRuleConfig(**values)


def _run(rule, prices=rising, minutes=20, latency_s=0, strategy_cls=BarRuleStrategy, quotes_from_min=0, **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(10000, USD)],
        base_currency=USD,
        latency_model=LatencyModel(base_latency_nanos=int(latency_s * 1_000_000_000)) if latency_s else None,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_data(prices, minutes, quotes_from_min))
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    strategy = strategy_cls(_config(**overrides), PauseGate(), blocks, records.append, rule)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows):
    return [r for r in rows if isinstance(r, DecisionRow)]


def _risk_events(rows):
    return [r for r in rows if isinstance(r, RiskEventRow)]


def _by_type(engine, order_type):
    return [o for o in engine.cache.orders() if o.order_type == order_type]


def _at(bar):
    """The close time of the strategy's ``bar``-th bar: the first bar closes at 12:00, on the first trade."""
    return _ns(START + timedelta(minutes=bar - 1))


def test_an_entry_floors_its_stop_rests_it_at_that_price_and_the_rule_s_exit_cancels_it():
    rule = Script(entries={5: (Side.LONG, 2)}, exits={7})
    engine, strategy, rows = _run(rule)

    enter, exit_ = _decisions(rows)
    assert (enter.intent, enter.side, enter.reason, enter.risk_verdict) == (
        Intent.ENTER,
        OrderSide.BUY,
        "script_long",
        RiskVerdict.APPROVED,
    )
    assert enter.decided_ns == _at(5)
    inputs = enter.inputs
    mid, atr = D(str(inputs["mid"])), D(str(inputs["atr"]))
    # 2 below the close is inside 1 ATR: the floor sets the stop, rounded down.
    assert inputs["floor_applied"] is True
    assert D(str(inputs["stop"])) == (mid - atr).to_integral_value(rounding=ROUND_FLOOR)
    assert D(str(inputs["structural_stop"])) == D(str(inputs["bar_close"])) - 2
    assert D(str(inputs["stop_px"])) == D(str(inputs["stop"]))  # the guard judged the stop actually used
    assert inputs["n"] == 5  # the rule's values
    assert {"bar_close", "mid", "atr", "structural_stop", "stop", "floor_applied", "quantity"} <= set(inputs)

    [stop] = _by_type(engine, OrderType.STOP_MARKET)
    assert D(str(stop.trigger_price)) == D(str(inputs["stop"]))  # the decided price, not an offset from the fill
    assert stop.is_reduce_only
    assert stop.status.name == "CANCELED"  # the exit cancelled it

    assert (exit_.intent, exit_.side, exit_.reason) == (Intent.EXIT, OrderSide.SELL, "target")
    assert exit_.decided_ns == _at(7)
    assert exit_.inputs["bars_held"] == 2
    assert rule.last_trade.bars_held == 2
    assert rule.last_trade.stop_px == D(str(inputs["stop"]))
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT
    assert rule.flats == 1
    assert rule.consumed_at == [5]


def test_the_entry_is_sized_from_the_stop_at_risk_per_trade_with_the_hold_s_funding_periods():
    _, _, rows = _run(Script(entries={5: (Side.LONG, 30)}, exits={7}))

    enter = _decisions(rows)[0]
    inputs = enter.inputs
    assert inputs["floor_applied"] is False  # 30 below the close is past 1 ATR
    equity, loss = D(str(inputs["equity"])), D(str(inputs["loss_per_unit"]))
    expected = (D("0.005") * equity / loss / D("0.0001")).to_integral_value(rounding=ROUND_FLOOR) * D("0.0001")
    assert D(str(inputs["risk_quantity"])) == expected
    assert inputs["funding_periods"] == funding_periods(enter.decided_ns, 10 * MINUTE_NS)


def test_a_very_tight_stop_is_clipped_to_the_order_cap():
    _, _, rows = _run(Script(entries={5: (Side.LONG, 2)}, exits={7}), max_order_notional_usd=D(1000))
    assert _decisions(rows)[0].inputs["limited_by"] == "notional_cap"


def test_a_stop_on_the_wrong_side_of_the_mid_is_refused_as_bad_signal_and_consumed():
    rule = Script(entries={5: (Side.LONG, -5)})  # 5 above the close
    engine, _, rows = _run(rule)

    [refused] = _decisions(rows)
    assert (refused.intent, refused.risk_verdict, refused.risk_reason) == (
        Intent.ENTER,
        RiskVerdict.REJECTED,
        "bad_signal",
    )
    assert engine.cache.orders() == []
    assert rule.consumed_at == [5]


def test_a_bar_close_with_no_quote_skips_the_entry_without_a_decision_and_consumes_the_signal():
    rule = Script(entries={5: (Side.LONG, 30), 8: (Side.LONG, 30)}, exits={10})
    _, _, rows = _run(rule, quotes_from_min=6)

    assert [d.decided_ns for d in _decisions(rows) if d.intent == Intent.ENTER] == [_at(8)]
    assert rule.consumed_at[:2] == [5, 8]


def test_a_position_still_open_after_time_stop_bars_exits_and_no_entry_goes_out_at_that_close():
    rule = Script(entries={5: (Side.LONG, 30), 8: (Side.LONG, 30)})
    engine, _, rows = _run(rule, time_stop_bars=3)

    assert [(d.intent, d.reason, d.decided_ns) for d in _decisions(rows)] == [
        (Intent.ENTER, "script_long", _at(5)),
        (Intent.EXIT, "time_stop", _at(8)),
    ]
    assert rule.consumed_at == [5, 8]  # bar 8's signal is dropped, not kept for later
    assert engine.cache.positions_open() == []


def test_a_signal_while_in_a_position_is_consumed_and_never_fires_after_it_closes():
    rule = Script(entries={5: (Side.LONG, 30), 6: (Side.LONG, 30)}, exits={7})
    _, _, rows = _run(rule)
    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER, Intent.EXIT]
    assert rule.consumed_at == [5, 6]


def test_a_fill_already_through_the_stop_exits_at_market_as_stop_through():
    # Venue messages take 20 s; the price falls 500 right after bar 5's close (12:04), so the long fills below its stop.
    def crash(t):
        return rising(t) if t < 4.05 else 84500

    rule = Script(entries={5: (Side.LONG, 2)})
    engine, strategy, rows = _run(rule, prices=crash, latency_s=20)

    enter, exit_ = _decisions(rows)
    assert (exit_.intent, exit_.reason) == (Intent.EXIT, "stop_through")
    assert D(str(exit_.inputs["entry_px"])) <= D(str(enter.inputs["stop"]))
    assert _by_type(engine, OrderType.STOP_MARKET) == []
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


class _StopAboveMarket(BarRuleStrategy):
    """Places the stop far above the market, so the venue rejects it."""

    def _submit_stop(self, entry):
        assert self._stop_px is not None
        self._stop_px = self._stop_px + 1000
        super()._submit_stop(entry)


def test_a_rejected_stop_blocks_entries_raises_a_critical_event_and_closes_at_market():
    rule = Script(entries={5: (Side.LONG, 30), 9: (Side.LONG, 30)})
    engine, strategy, rows = _run(rule, strategy_cls=_StopAboveMarket)

    [stop] = _by_type(engine, OrderType.STOP_MARKET)
    assert stop.status.name == "REJECTED"
    (event,) = [e for e in _risk_events(rows) if e.kind == RiskEventKind.ORDER_REJECTED]
    assert (event.severity, event.event_key) == (Severity.CRITICAL, f"stop_not_accepted:{stop.client_order_id}")
    assert strategy._blocks.active() == (BlockReason.STOP_NOT_ACCEPTED,)
    assert engine.cache.positions_open() == []
    later = _decisions(rows)[-1]
    assert (later.decided_ns, later.risk_reason) == (_at(9), "blocked:stop_not_accepted")


class _RefusedExits(BarRuleStrategy):
    """The first ``refusals`` exits are refused before they reach the venue."""

    refusals = 90

    def _submit_exit(self, event=None):
        if self._position() is not None and type(self).refusals > 0:
            type(self).refusals -= 1
            self._exit_refused("rejected: test")
            return
        super()._submit_exit(event)


def test_a_refused_exit_retries_every_second_and_bar_closes_meanwhile_send_nothing():
    _RefusedExits.refusals = 90  # 90 s of refusals from bar 7 (12:06) spans the bar 8 and 9 closes
    rule = Script(entries={5: (Side.LONG, 30), 8: (Side.LONG, 30)}, exits={7, 8, 9})
    engine, strategy, rows = _run(rule, strategy_cls=_RefusedExits)

    assert [(d.intent, d.decided_ns) for d in _decisions(rows)] == [(Intent.ENTER, _at(5)), (Intent.EXIT, _at(7))]
    refusals = [e for e in _risk_events(rows) if e.event_key.startswith("exit_refused:")]
    assert len(refusals) == 90
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


class _FlattenAt(BarRuleStrategy):
    """Calls ``flatten`` at 12:06:30."""

    def on_start(self):
        super().on_start()
        self.clock.set_time_alert_ns(
            "test_flatten", _ns(START + timedelta(minutes=6, seconds=30)), lambda _: self.flatten(HALT_ID)
        )


def test_flatten_closes_the_position_with_kill_switch_tags_and_no_decision():
    rule = Script(entries={5: (Side.LONG, 30)})
    engine, strategy, rows = _run(rule, strategy_cls=_FlattenAt)

    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER]
    _, exit_order = _by_type(engine, OrderType.MARKET)
    assert exit_order.is_reduce_only
    assert exit_order.tags == [f"{ORIGIN_TAG}kill_switch", f"{HALT_TAG}{HALT_ID}"]
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT
    assert rule.flats == 1


def test_a_stop_fill_resets_the_trade_state_and_calls_the_rule_s_on_flat():
    def drop(t):
        return rising(t) if t < 6.5 else 84000

    rule = Script(entries={5: (Side.LONG, 30)})
    engine, strategy, rows = _run(rule, prices=drop)

    [stop] = _by_type(engine, OrderType.STOP_MARKET)
    assert stop.status.name == "FILLED"
    assert [d.intent for d in _decisions(rows)] == [Intent.ENTER]
    assert engine.cache.positions_open() == []
    assert (strategy.phase, strategy.bars_held) == (Phase.FLAT, 0)
    assert rule.flats == 1


def test_no_decision_before_the_rule_and_the_atr_are_ready():
    rule = Script(entries={1: (Side.LONG, 30), 2: (Side.LONG, 30)}, warm=1)
    _, _, rows = _run(rule, atr_period=3)  # the ATR is ready only from bar 4
    assert _decisions(rows) == []
    assert rule.atrs[:3] == [None, None, None]
    assert rule.consumed_at == []


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("time_stop_bars", 0, "time_stop_bars"),
        ("atr_period", 1, "atr_period"),
        ("atr_floor_mult", D(0), "atr_floor_mult"),
        ("bar_spec", "1-MINUTE-MID", "bar_spec"),
        ("bar_spec", "1-TICK-LAST", "bar_spec"),
    ],
)
def test_the_base_config_refuses_bad_values(field, value, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**{field: value})


def test_the_bar_interval_follows_the_bar_spec():
    assert bar_interval_ns("1-HOUR-LAST") == 60 * MINUTE_NS
    assert bar_interval_ns("4-HOUR-LAST") == 240 * MINUTE_NS
    assert bar_interval_ns("15-MINUTE-LAST") == 15 * MINUTE_NS
