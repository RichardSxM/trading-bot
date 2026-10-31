"""covers: spec 0017 AC-6 (the regime block: unwarm and trending ADX block, calm clears, lifted on stop) and AC-5
(the scalping base: maker and taker entries, the entry timeout and partial fills, the
stop following each fill, the target, time stop, and stop exits, late entry fills, and the spread cap,
reward floor, and stop side refusals), and AC-18 (a stop already through at the fill exits with no block).

Runs a scripted ``ScalpStrategy`` child in a BacktestEngine on synthetic quotes, with 1 minute MID bars
built from them. Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderStatus, OrderType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_placeholder import INSTRUMENT, INSTRUMENT_ID, START, _ns

from trading_core.node.funding import FundingLedger
from trading_core.node.recording import RecordingActor
from trading_core.node.scalp_base import Phase, ScalpConfig, ScalpStrategy
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env, Intent, OrderSide, RiskEventKind, RiskVerdict, Severity
from trading_core.recording.rows import DecisionRow, RiskEventRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.scalp import ScalpSignal

D = Decimal


class _Scripted(ScalpStrategy):
    """Signals on the bars ``script`` names (1 = the first bar): side, stop and target offsets from the close."""

    script: ClassVar[dict[int, tuple[OrderSide, int, int]]] = {}

    def __init__(self, *args):
        super().__init__(*args)
        self.bars = 0

    @property
    def entry_reason(self):
        return "scripted_entry"

    def signal_on_bar(self, bar):
        self.bars += 1
        planned = type(self).script.get(self.bars)
        if planned is None:
            return None
        side, stop, target = planned
        close = bar.close.as_decimal()
        return ScalpSignal(side, close + stop, close + target, {"bar": self.bars})


class _StuckCancel(_Scripted):
    """The entry's cancel never reaches the venue, so its remainder keeps filling: every fill is late."""

    def cancel_order(self, order, *args, **kwargs):
        if order.client_order_id == self._entry_id:
            return
        super().cancel_order(order, *args, **kwargs)


class _NoThroughCheck(_Scripted):
    """Skips the AC-18 check, so a stop in the market reaches the venue and is rejected."""

    def _stop_through(self, position):
        return False


class _RealReject(_NoThroughCheck):
    """The next quote never shows the rejected stop through, so the rejection is a real one."""

    def _rejected_stop_through(self, rejected, bid, ask):
        return False


def _quotes(mid, minutes, size="5.0000", every_s=10):
    """A quote every ``every_s`` seconds; ``mid(t)`` gives the mid at t minutes; the spread is 2."""
    ticks = []
    for step in range(int(minutes * 60 / every_s)):
        at = START + timedelta(seconds=step * every_s)
        m = mid(step * every_s / 60)
        ticks.append(
            QuoteTick(
                instrument_id=INSTRUMENT_ID,
                bid_price=Price(m - 1, 0),
                ask_price=Price(m + 1, 0),
                bid_size=Quantity.from_str(size),
                ask_size=Quantity.from_str(size),
                ts_event=_ns(at),
                ts_init=_ns(at),
            )
        )
    return ticks


def _run(mid, minutes, script, strategy_cls=_Scripted, size="5.0000", consume=False, **overrides):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(1000, USD)],
        base_currency=USD,
        # Each quote's size is used up by fills, so a thin touch gives partial fills.
        liquidity_consumption=consume,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_quotes(mid, minutes, size))
    run_id = uuid.uuid7()
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    engine.add_actor(
        RecordingActor(
            env=Env.DEMO,
            run_id=run_id,
            instrument_ids=[INSTRUMENT_ID],
            record=records.append,
            blocks=blocks,
            maintenance_margin_rate=D("0.01"),
            min_liq_distance_pct=D(10),
            stale_quote_s=D(300),
            ledger=FundingLedger(),
        )
    )
    values = {
        "env": "demo",
        "run_id": str(run_id),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "1-MINUTE-MID",
        "time_stop_min": 5,
        "use_market_context": False,
        "max_order_notional_usd": D(250),
        "max_leverage": D(2),
        "max_total_exposure_usd": D(500),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
    } | overrides
    strategy_cls.script = script
    config = ScalpConfig(**values)  # type: ignore[arg-type]  # each value has its own type
    strategy = strategy_cls(config, PauseGate(), blocks, records.append)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows, intent=None):
    return [r for r in rows if isinstance(r, DecisionRow) and (intent is None or r.intent == intent)]


def _orders(engine, order_type):
    return [o for o in engine.cache.orders() if o.order_type == order_type]


# No funding boundary inside a short hold that starts at 12:01 (the context is off in these tests).
FLAT_MARKET = 85_000
LONG = {1: (OrderSide.BUY, -200, 300)}


def test_an_unfilled_maker_entry_is_cancelled_at_its_timeout_and_the_strategy_is_flat_again():
    engine, strategy, rows = _run(lambda t: 85_000 + int(t * 60), 4, LONG)

    (entry,) = _orders(engine, OrderType.LIMIT)
    assert entry.is_post_only
    assert entry.status == OrderStatus.CANCELED
    assert entry.filled_qty == 0
    assert _orders(engine, OrderType.STOP_MARKET) == []
    assert strategy.phase == Phase.FLAT
    (decision,) = _decisions(rows)
    assert decision.risk_verdict == RiskVerdict.APPROVED
    # Placed at the best bid of the quote at the bar close, cancelled 30 s later.
    assert entry.price.as_decimal() == D(decision.inputs["mid"]) - 1
    canceled = entry.events[-1]
    assert canceled.ts_event - decision.decided_ns == 30_000_000_000
    assert {"mid", "spread_bps", "stop_px", "target_px", "bar"} <= set(decision.inputs)


def test_a_partial_maker_fill_is_kept_and_the_stop_follows_every_fill():
    # Falling, with only 0.001 BTC at the touch: the resting bid fills a little each quote until its timeout.
    engine, _strategy, rows = _run(
        lambda t: 85_000 - int(t * 30),
        9,
        {1: (OrderSide.BUY, -1500, 3000)},
        size="0.0010",
        consume=True,
        entry_timeout_s=25,
    )

    (entry,) = _orders(engine, OrderType.LIMIT)
    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert entry.status == OrderStatus.CANCELED
    # Two quotes inside the timeout, 0.001 each: the stop was placed at the first and grown at the second.
    assert entry.filled_qty == Quantity.from_str("0.0020") < entry.quantity
    assert stop.is_reduce_only
    assert stop.quantity == entry.filled_qty
    (decision,) = _decisions(rows, Intent.ENTER)
    assert stop.trigger_price.as_decimal() == D(decision.inputs["stop_px"])
    # The time stop closed it (5 minutes after the first fill) and the stop went with it.
    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "time_stop"
    assert stop.status == OrderStatus.CANCELED
    assert engine.cache.positions_open() == []


def test_a_target_touch_by_the_bid_exits_a_long_and_cancels_the_stop():
    engine, strategy, rows = _run(
        lambda t: 85_000 + int(t * 60), 6, {1: (OrderSide.BUY, -200, 100)}, entry_mode="taker"
    )

    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "target"
    _entry, exit_order = _orders(engine, OrderType.MARKET)
    assert exit_order.is_reduce_only
    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert stop.status == OrderStatus.CANCELED
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_the_time_stop_exits_time_stop_min_after_the_first_fill():
    engine, _strategy, rows = _run(lambda t: FLAT_MARKET, 9, LONG, entry_mode="taker", time_stop_min=3)

    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "time_stop"
    entry, _ = _orders(engine, OrderType.MARKET)
    first_fill = entry.ts_last
    assert exit_decision.decided_ns == first_fill + 3 * 60_000_000_000


def test_a_stop_fill_is_the_stop_exit_and_leaves_no_exit_order_behind():
    engine, strategy, rows = _run(
        lambda t: 85_000 - int(t * 100), 6, {1: (OrderSide.BUY, -100, 300)}, entry_mode="taker"
    )

    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert stop.status == OrderStatus.FILLED
    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "stop"
    assert [o for o in _orders(engine, OrderType.MARKET) if o.is_reduce_only] == []
    assert engine.cache.orders_open() == []
    assert strategy.phase == Phase.FLAT


# The bid is the mid less 1, so a long's stop 1 under the close is already through when the entry fills.
THROUGH_THEN_LONG = {1: (OrderSide.BUY, -1, 300), 3: (OrderSide.BUY, -200, 300)}


@pytest.mark.parametrize(
    ("mid", "script", "entry_mode"),
    [
        (lambda t: FLAT_MARKET, THROUGH_THEN_LONG, "taker"),
        (lambda t: FLAT_MARKET, {1: (OrderSide.SELL, 1, -300), 3: (OrderSide.SELL, 200, -300)}, "taker"),
        # Falling: the resting bid fills when the market trades through it, so the bid is then below the stop.
        (lambda t: 85_000 - int(t * 30), THROUGH_THEN_LONG, "maker"),
    ],
)
def test_a_stop_already_through_at_the_fill_exits_at_once_and_blocks_nothing(mid, script, entry_mode):
    engine, strategy, rows = _run(mid, 6, script, entry_mode=entry_mode)

    first, second = _decisions(rows, Intent.ENTER)
    assert (first.risk_verdict, second.risk_verdict) == (RiskVerdict.APPROVED, RiskVerdict.APPROVED)
    (exit_decision, *_) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "stop_through"
    assert {"bid", "ask", "avg_px", "stop_px"} <= set(exit_decision.inputs)
    # No stop for the first entry; the second one, with room, got its stop.
    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert stop.trigger_price.as_decimal() == D(second.inputs["stop_px"])
    assert BlockReason.STOP_NOT_ACCEPTED not in strategy._blocks.active()
    assert not [r for r in rows if isinstance(r, RiskEventRow) and r.severity == Severity.CRITICAL]


def test_a_rejected_stop_the_next_quote_shows_through_exits_and_blocks_nothing():
    # Without the fill check the stop reaches the venue, which rejects it as in the market, as when the
    # venue judged it by a quote the strategy had not seen yet; that quote then settles it.
    engine, strategy, rows = _run(
        lambda t: FLAT_MARKET, 6, THROUGH_THEN_LONG, strategy_cls=_NoThroughCheck, entry_mode="taker"
    )

    assert BlockReason.STOP_NOT_ACCEPTED not in strategy._blocks.active()
    assert not [r for r in rows if isinstance(r, RiskEventRow) and r.severity == Severity.CRITICAL]
    (exit_decision, *_) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "stop_through"
    assert {"bid", "ask", "stop_px"} <= set(exit_decision.inputs)
    first, second = _decisions(rows, Intent.ENTER)
    assert (first.risk_verdict, second.risk_verdict) == (RiskVerdict.APPROVED, RiskVerdict.APPROVED)
    # The first stop was rejected; the second entry, with room, got one that rests.
    rejected, resting = _orders(engine, OrderType.STOP_MARKET)
    assert (rejected.status, resting.status) == (OrderStatus.REJECTED, OrderStatus.ACCEPTED)


def test_a_rejected_stop_whose_through_quote_came_first_exits_and_blocks_nothing():
    # As in the candle lab: one quote dips through the stop and fills the resting entry, the strategy
    # sees that quote before the rejection, and the next quote is back above the stop.
    engine, strategy, rows = _run(
        lambda t: 84_700 if abs(t - 2.5) < 1e-9 else FLAT_MARKET,
        7,
        {3: (OrderSide.BUY, -200, 300), 5: (OrderSide.BUY, -200, 300)},
    )

    (rejected,) = _orders(engine, OrderType.STOP_MARKET)
    assert rejected.status == OrderStatus.REJECTED
    assert BlockReason.STOP_NOT_ACCEPTED not in strategy._blocks.active()
    assert not [r for r in rows if isinstance(r, RiskEventRow) and r.severity == Severity.CRITICAL]
    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "stop_through"
    assert D(exit_decision.inputs["bid"]) <= D(exit_decision.inputs["stop_px"])
    first, second = _decisions(rows, Intent.ENTER)
    assert (first.risk_verdict, second.risk_verdict) == (RiskVerdict.APPROVED, RiskVerdict.APPROVED)


def test_a_stop_the_venue_rejects_for_real_still_blocks_entries():
    _engine, strategy, rows = _run(
        lambda t: FLAT_MARKET, 6, THROUGH_THEN_LONG, strategy_cls=_RealReject, entry_mode="taker"
    )

    assert BlockReason.STOP_NOT_ACCEPTED in strategy._blocks.active()
    (event,) = [r for r in rows if isinstance(r, RiskEventRow) and r.severity == Severity.CRITICAL]
    assert event.kind == RiskEventKind.ORDER_REJECTED
    _first, second = _decisions(rows, Intent.ENTER)
    assert second.risk_reason == "blocked:stop_not_accepted"


def test_a_late_entry_fill_after_the_exit_is_closed_at_once():
    # The entry's cancel is lost, so after the target exit its remainder keeps filling.
    engine, _strategy, rows = _run(
        # Down to fill a little, up through the target, then down through the stuck bid again.
        lambda t: (
            85_000 - int(t * 30)
            if t < 0.4
            else 85_000 + int((t - 0.4) * 400)
            if t < 2
            else 85_640 - int((t - 2) * 1000)
        ),
        5,
        {1: (OrderSide.BUY, -1500, 50)},
        strategy_cls=_StuckCancel,
        size="0.0010",
        consume=True,
        entry_timeout_s=600,
    )

    reasons = [d.reason for d in _decisions(rows, Intent.EXIT)]
    assert "target" in reasons
    assert "late_entry_fill" in reasons
    assert engine.cache.positions_open() == []


@pytest.mark.parametrize(
    ("script", "overrides", "reason"),
    [
        (LONG, {"max_spread_bps": D("0.1")}, "spread_cap"),
        (LONG, {"min_target_bps": D(100)}, "target_too_close"),
        ({1: (OrderSide.BUY, 50, 300)}, {}, "invalid_stop"),
    ],
)
def test_a_refused_signal_records_the_rejection_at_info_and_places_nothing(script, overrides, reason):
    engine, _strategy, rows = _run(lambda t: FLAT_MARKET, 3, script, **overrides)

    (decision,) = _decisions(rows)
    assert (decision.risk_verdict, decision.risk_reason) == (RiskVerdict.REJECTED, reason)
    (event,) = [r for r in rows if isinstance(r, RiskEventRow)]
    assert (event.kind, event.severity, event.decision_id) == (RiskEventKind.LIMIT_HIT, Severity.INFO, decision.id)
    assert engine.cache.orders() == []


REGIME = {"regime_adx_max": D(25), "regime_adx_period": 2, "regime_bar_spec": "5-MINUTE-MID"}


def test_regime_blocks_until_the_adx_is_warm_then_a_calm_market_clears_it():
    # Two 5 minute bars of ADX(2) warmth are not enough; four are (20 minutes).
    script = {3: (OrderSide.BUY, -200, 300), 25: (OrderSide.BUY, -200, 300)}
    _engine, strategy, rows = _run(lambda t: FLAT_MARKET, 27, script, entry_mode="taker", **REGIME)

    early, late = _decisions(rows, Intent.ENTER)
    assert (early.risk_verdict, early.risk_reason, early.inputs["adx"]) == (
        RiskVerdict.REJECTED,
        "blocked:regime",
        None,
    )
    assert late.risk_verdict == RiskVerdict.APPROVED
    assert D(late.inputs["adx"]) == 0
    assert "regime" not in strategy._blocks.active()  # lifted on stop at the end of the run


def test_a_trending_market_keeps_regime_on():
    script = {25: (OrderSide.BUY, -200, 3000)}
    _engine, _strategy, rows = _run(lambda t: 85_000 + int(t * 50), 27, script, entry_mode="taker", **REGIME)

    (decision,) = _decisions(rows, Intent.ENTER)
    assert decision.risk_reason == "blocked:regime"
    assert D(decision.inputs["adx"]) > 25


def test_without_regime_adx_max_no_regime_bars_are_subscribed():
    _engine, strategy, rows = _run(lambda t: FLAT_MARKET, 3, LONG, entry_mode="taker")

    assert strategy.regime_bar_type is None
    assert "adx" not in _decisions(rows)[0].inputs


# AC-19: maker target exits ---------------------------------------------------------------------

MAKER_EXIT = {"exit_mode": "maker"}


class _LoseTarget(_Scripted):
    """Something other than the base cancels the resting target as soon as the venue accepts it."""

    def on_order_accepted(self, event):
        super().on_order_accepted(event)
        if event.client_order_id == self._target_id:
            super().cancel_order(self.cache.order(event.client_order_id))


def _targets(engine):
    return [o for o in _orders(engine, OrderType.LIMIT) if o.is_reduce_only]


def test_a_maker_target_rests_after_the_stop_and_its_fill_is_the_target_exit():
    engine, strategy, rows = _run(
        lambda t: 85_000 + int(t * 60), 6, {1: (OrderSide.BUY, -200, 100)}, entry_mode="taker", **MAKER_EXIT
    )

    (target,) = _targets(engine)
    assert target.is_post_only
    assert target.status == OrderStatus.FILLED
    (enter,) = _decisions(rows, Intent.ENTER)
    assert target.price.as_decimal() == D(enter.inputs["target_px"])
    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert stop.ts_init <= target.ts_init
    assert stop.status == OrderStatus.CANCELED
    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "target"
    # No market exit: only the entry went out at market.
    assert [o for o in _orders(engine, OrderType.MARKET) if o.is_reduce_only] == []
    assert engine.cache.orders_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_post_only_rejected_target_exits_at_market_with_one_target_decision():
    # A long's target at the bid would trade at once, so the venue rejects it as post only.
    engine, strategy, rows = _run(
        lambda t: FLAT_MARKET, 4, {1: (OrderSide.BUY, -200, -1)}, entry_mode="taker", **MAKER_EXIT
    )

    (target,) = _targets(engine)
    assert target.status == OrderStatus.REJECTED
    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == "target"
    assert exit_decision.inputs["target_refused"].startswith("rejected")
    (_entry, exit_order) = _orders(engine, OrderType.MARKET)
    assert exit_order.is_reduce_only
    assert engine.cache.positions_open() == []
    assert engine.cache.orders_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_target_cancelled_by_someone_else_exits_at_market():
    engine, _strategy, rows = _run(
        lambda t: FLAT_MARKET, 4, LONG, strategy_cls=_LoseTarget, entry_mode="taker", **MAKER_EXIT
    )

    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert (exit_decision.reason, exit_decision.inputs["target_refused"]) == ("target", "canceled")
    assert engine.cache.positions_open() == []
    assert engine.cache.orders_open() == []


@pytest.mark.parametrize(
    ("mid", "script", "reason"),
    [
        (lambda t: FLAT_MARKET, LONG, "time_stop"),
        (lambda t: 85_000 - int(t * 100), {1: (OrderSide.BUY, -100, 300)}, "stop"),
    ],
)
def test_the_time_stop_and_a_stop_fill_cancel_the_target_and_leave_no_order_behind(mid, script, reason):
    engine, strategy, rows = _run(mid, 9, script, entry_mode="taker", **MAKER_EXIT)

    (exit_decision,) = _decisions(rows, Intent.EXIT)
    assert exit_decision.reason == reason
    (target,) = _targets(engine)
    assert target.status == OrderStatus.CANCELED
    assert engine.cache.orders_open() == []
    assert engine.cache.positions_open() == []
    assert strategy.phase == Phase.FLAT


def test_a_partial_target_fill_shrinks_the_stop_and_records_one_target_decision():
    # A thin touch: the maker entry fills 0.001 twice before its timeout, then price rises through the
    # target, which fills 0.001 a quote.
    engine, _strategy, rows = _run(
        lambda t: 85_000 - int(t * 30) if t < 1.5 else 84_955 + int((t - 1.5) * 400),
        5,
        {1: (OrderSide.BUY, -1500, 50)},
        size="0.0010",
        consume=True,
        entry_timeout_s=25,
        **MAKER_EXIT,
    )

    (target,) = _targets(engine)
    assert target.status == OrderStatus.FILLED
    assert target.quantity == Quantity.from_str("0.0020")
    (stop,) = _orders(engine, OrderType.STOP_MARKET)
    assert Quantity.from_str("0.0010") in [e.quantity for e in stop.events if type(e).__name__ == "OrderUpdated"]
    assert [d.reason for d in _decisions(rows, Intent.EXIT)] == ["target"]
    assert engine.cache.positions_open() == []


def test_without_exit_mode_no_target_order_rests():
    engine, _strategy, rows = _run(
        lambda t: 85_000 + int(t * 60), 6, {1: (OrderSide.BUY, -200, 100)}, entry_mode="taker"
    )

    assert _targets(engine) == []
    assert [d.reason for d in _decisions(rows, Intent.EXIT)] == ["target"]
