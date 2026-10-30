"""covers: spec 0023 AC-7 to AC-12 (what each finding's action is), AC-9 (the stop replace rules), AC-23 (demo never
acts), AC-28 (while flattening or halted only unknown entries are cancelled, fills are still recovered, and Kraken
is still closed for an untrusted instrument), AC-8 (``cache_fix: manual``), AC-11 (fill recovery), for the pure
``plan``."""

from decimal import Decimal

from test_reconcile_compare import BTC, ETH, OURS, TRIGGER, _exchange, _held, _local, _order, _position, _stop

from trading_core.reconcile.compare import compare
from trading_core.reconcile.plan import (
    CancelOrder,
    CloseAtMarket,
    CloseResidual,
    MarkCanceled,
    MarkUntrusted,
    Mode,
    Phase,
    PlaceStop,
    RecoverFills,
    SyncPosition,
    plan,
)
from trading_core.reconcile.recover import recover
from trading_core.reconcile.views import FillView
from trading_core.recording.enums import MismatchAction, MismatchKind, OrderSide, PositionSide

ABOVE = {BTC: Decimal("65000")}  # a long's stop at 60000 is not crossed
BELOW = {BTC: Decimal("59000")}


def _plan(exchange, local, mode=Mode.ACT, mids=None, **kw):
    return plan(compare(exchange, local), local, mode, mids, **kw)


def _one(the_plan):
    (planned,) = the_plan.planned
    return planned


def test_a_clean_check_plans_nothing():
    the_plan = _plan(_exchange(), _local())

    assert the_plan.planned == ()
    assert not the_plan.halts


def test_an_unknown_order_is_cancelled_without_a_halt():
    """AC-10."""
    foreign = _order("drill-foreign-1", "V-7", strategy_id=None)

    planned = _one(_plan(_exchange([foreign]), _local()))

    assert planned.action is MismatchAction.CANCELLED
    assert planned.steps == (CancelOrder(order=foreign),)
    assert not planned.halts


def test_a_local_only_order_is_marked_canceled():
    """AC-11."""
    planned = _one(_plan(_exchange(), _local([_order()])))

    assert planned.action is MismatchAction.MARKED_CANCELED
    assert planned.steps == (MarkCanceled(order=_order()),)
    assert not planned.halts


def test_an_unknown_position_is_synced_into_the_cache_flattened_and_halts():
    """AC-7: Nautilus adds it as ``EXTERNAL``, then the executor closes it."""
    planned = _one(_plan(_exchange(positions=[_position()]), _local()))

    assert planned.finding.kind is MismatchKind.UNKNOWN_POSITION
    assert planned.action is MismatchAction.FLATTENED
    assert planned.steps == (SyncPosition(instrument_id=BTC, exchange=_position()),)
    assert (planned.halts, planned.flattens) == (True, True)
    assert planned.details == {"cache_fix": "synced"}


def test_more_on_the_exchange_than_in_the_cache_is_synced_too():
    planned = _one(_plan(_exchange(positions=[_position(qty="0.002")]), _local(positions=[_held()])))

    assert planned.steps == (SyncPosition(instrument_id=BTC, exchange=_position(qty="0.002")),)
    assert planned.flattens


def test_a_cache_position_the_exchange_does_not_have_is_marked_untrusted_and_halts():
    """AC-8: Nautilus 1.231.0 cannot shrink a strategy's position by report, so the cache waits for a hand reset."""
    planned = _one(_plan(_exchange(), _local(positions=[_held()])))

    assert planned.action is MismatchAction.NONE  # Kraken holds nothing to close
    assert planned.steps == (MarkUntrusted(instrument_id=BTC), CloseResidual(instrument_id=BTC))
    assert (planned.halts, planned.flattens) == (True, True)
    assert planned.details == {"cache_fix": "manual"}


def test_less_on_the_exchange_than_in_the_cache_closes_what_kraken_holds_and_halts():
    planned = _one(_plan(_exchange(positions=[_position(qty="0.0005")]), _local(positions=[_held()])))

    assert planned.action is MismatchAction.FLATTENED
    assert planned.steps == (MarkUntrusted(instrument_id=BTC), CloseResidual(instrument_id=BTC))
    assert (planned.halts, planned.flattens) == (True, True)
    assert planned.details == {"cache_fix": "manual"}


def test_the_other_side_on_the_exchange_is_manual_too():
    exchange = _exchange(positions=[_position(side=PositionSide.SHORT)])

    planned = _one(_plan(exchange, _local(positions=[_held()])))

    assert planned.details == {"cache_fix": "manual"}


def test_a_local_only_order_with_missed_fills_is_recovered():
    """AC-11."""
    order = _order()
    fill = _fill()
    (recovery,) = recover([order], [fill], since_ns=0)

    planned = _one(_plan(_exchange(), _local([order]), recoveries={"O-1": recovery}))

    assert planned.action is MismatchAction.ADOPTED
    assert planned.steps == (RecoverFills(recovery=recovery),)
    assert planned.details == {"cache_fix": "recovered", "fills": "1", "filled_qty": "0.001", "order_status": "filled"}
    assert not planned.halts


def test_a_partly_filled_recovery_ends_canceled():
    order = _order()
    (recovery,) = recover([order], [_fill(qty="0.0004")], since_ns=0)

    planned = _one(_plan(_exchange(), _local([order]), recoveries={"O-1": recovery}))

    assert (planned.details["order_status"], planned.details["filled_qty"]) == ("canceled", "0.0004")


def test_a_position_that_is_not_ours_but_adds_up_is_flattened_with_no_sync():
    planned = _one(_plan(_exchange(positions=[_position()]), _local(positions=[_held(strategy="EXTERNAL")])))

    assert planned.steps == ()
    assert (planned.action, planned.halts, planned.flattens) == (MismatchAction.FLATTENED, True, True)


def test_a_duplicate_entry_cancels_every_open_entry_and_halts():
    """AC-12."""
    entries = [_order(), _order("O-2", "V-2")]

    planned = _one(_plan(_exchange([*entries, _stop()]), _local([*entries, _stop()])))

    assert planned.action is MismatchAction.FLATTENED
    assert planned.steps == tuple(CancelOrder(order=o) for o in entries)
    assert (planned.halts, planned.flattens) == (True, True)


def test_a_missing_stop_is_placed_at_the_recorded_price_for_the_full_size():
    """AC-9."""
    planned = _one(_plan(_exchange(positions=[_position()]), _local(positions=[_held()]), mids=ABOVE))

    assert planned.action is MismatchAction.STOP_REPLACED
    (step,) = planned.steps
    assert step == PlaceStop(
        instrument_id=BTC,
        position_id=f"{BTC}-{OURS}",
        side=OrderSide.SELL,
        quantity=Decimal("0.001"),
        trigger_price=TRIGGER,
        parent_client_order_id="E-1",
        then_cancel=(),
    )
    assert not planned.halts


def test_a_short_gets_a_buy_stop():
    local = _local(positions=[_held(side=PositionSide.SHORT)], triggers={BTC: Decimal("70000")})

    planned = _one(_plan(_exchange(positions=[_position(side=PositionSide.SHORT)]), local, mids=ABOVE))

    (step,) = planned.steps
    assert (step.side, step.trigger_price) == (OrderSide.BUY, Decimal("70000"))


def test_a_wrong_size_stop_is_replaced_and_the_old_one_cancelled_only_after():
    """AC-9: the cancels ride on the place step, which runs them once Kraken accepts it."""
    half = _stop(qty="0.0005")

    planned = _one(_plan(_exchange([half], [_position()]), _local([half], [_held()]), mids=ABOVE))

    (step,) = planned.steps
    assert isinstance(step, PlaceStop)
    assert step.then_cancel == (half,)


def test_when_the_target_stop_is_already_there_only_the_others_are_cancelled():
    """AC-9: safe to repeat after a crash between the two steps."""
    target, half = _stop(), _stop("S-2", "VS-2", qty="0.0005")

    planned = _one(_plan(_exchange([target, half], [_position()]), _local([target, half], [_held()])))

    assert planned.action is MismatchAction.STOP_REPLACED
    assert planned.steps == (CancelOrder(order=half),)
    assert not planned.awaiting_quote  # no quote needed: nothing is placed


def test_a_crossed_trigger_closes_at_market_without_a_halt():
    """AC-9: it did what the stop would have done."""
    planned = _one(_plan(_exchange(positions=[_position()]), _local(positions=[_held()]), mids=BELOW))

    assert planned.action is MismatchAction.CLOSED_AT_MARKET
    assert planned.steps == (CloseAtMarket(instrument_id=BTC, position_id=f"{BTC}-{OURS}"),)
    assert not planned.halts


def test_a_short_is_crossed_when_the_mid_is_at_or_above_the_trigger():
    local = _local(positions=[_held(side=PositionSide.SHORT)], triggers={BTC: Decimal("70000")})

    planned = _one(_plan(_exchange(positions=[_position(side=PositionSide.SHORT)]), local, mids={BTC: Decimal(70000)}))

    assert planned.action is MismatchAction.CLOSED_AT_MARKET


def test_without_a_fresh_quote_the_stop_fix_waits_then_flattens_and_halts():
    """AC-9."""
    exchange, local = _exchange(positions=[_position()]), _local(positions=[_held()])

    waiting = _plan(exchange, local)
    assert waiting.awaiting_quote == frozenset({BTC})
    assert not waiting.halts

    planned = _one(_plan(exchange, local, quote_waited=True))
    assert (planned.action, planned.halts, planned.flattens) == (MismatchAction.FLATTENED, True, True)
    assert planned.details == {"why": "no_fresh_quote"}


def test_no_recorded_trigger_price_flattens_and_halts():
    planned = _one(_plan(_exchange(positions=[_position()]), _local(positions=[_held()], triggers={}), mids=ABOVE))

    assert (planned.halts, planned.details) == (True, {"why": "no_trigger_price"})


def test_stops_on_different_prices_flatten_and_halt():
    stops = [_stop(), _stop("S-2", "VS-2", trigger=Decimal("59000"))]

    planned = _one(_plan(_exchange(stops, [_position()]), _local(stops, [_held()]), mids=ABOVE))

    assert (planned.halts, planned.details) == (True, {"why": "stop_prices_disagree"})


def test_a_halting_finding_skips_every_stop_fix():
    """The flatten closes the position, so no stop is placed first."""
    exchange = _exchange(positions=[_position(), _position("PF_ETHUSD.KRAKEN")])
    local = _local(positions=[_held()])

    the_plan = _plan(exchange, local, mids=ABOVE)

    kinds = {p.finding.kind: p for p in the_plan.planned}
    stop = kinds[MismatchKind.MISSING_STOP]
    assert (stop.steps, stop.halts, stop.details) == ((), True, {"why": "halting_finding"})
    assert the_plan.halts
    assert the_plan.flattens


def test_steps_run_in_phase_order():
    foreign = _order("F-1", "VF-1", strategy_id=None)
    local_only = _order("L-1", "VL-1")
    exchange = _exchange([foreign], [_position("PF_ETHUSD.KRAKEN")])

    the_plan = _plan(exchange, _local([local_only]))

    phases = [step.phase for _, step in the_plan.steps()]
    assert phases == [Phase.CANCEL, Phase.MARK, Phase.SYNC]


def test_recovery_runs_after_the_cancels_and_before_the_marks():
    foreign = _order("F-1", "VF-1", strategy_id=None)
    recovered, marked = _order("R-1", "VR-1"), _order("L-1", "VL-1", ETH)
    (recovery,) = recover([recovered], [_fill(venue="VR-1")], since_ns=0)

    the_plan = _plan(_exchange([foreign]), _local([marked, recovered]), recoveries={"R-1": recovery})

    assert [step.phase for _, step in the_plan.steps()] == [Phase.CANCEL, Phase.RECOVER, Phase.MARK]
    assert the_plan.recovers


def test_demo_never_acts():
    """AC-23."""
    the_plan = _plan(_exchange([_order("F-1", "VF-1")], [_position()]), _local(), mode=Mode.DEMO)

    assert all(p.action is MismatchAction.NONE and not p.steps and not p.halts for p in the_plan.planned)


def test_while_flattening_or_halted_only_unknown_entries_are_cancelled():
    """AC-28: no stop is touched, nothing is closed, no second halt."""
    foreign_entry = _order("F-1", "VF-1", strategy_id=None)
    foreign_stop = _stop("F-2", "VF-2")
    exchange = _exchange([foreign_entry, foreign_stop], [_position(), _position("PF_ETHUSD.KRAKEN")])

    the_plan = _plan(exchange, _local(positions=[_held()]), mode=Mode.REPORT, mids=ABOVE)

    acted = [(p.finding.kind, p.finding.client_order_id) for p in the_plan.planned if p.steps]
    assert acted == [(MismatchKind.UNKNOWN_ORDER, "F-1")]
    assert not the_plan.halts


def test_while_halted_fills_are_still_recovered_and_kraken_still_closed():
    """AC-28: recovery and the residual close only correct the books and Kraken; nothing halts again."""
    order = _order()
    (recovery,) = recover([order], [_fill()], since_ns=0)
    exchange = _exchange(positions=[_position(qty="0.0005")])

    the_plan = _plan(exchange, _local([order], [_held()]), mode=Mode.REPORT, recoveries={"O-1": recovery})

    steps = {p.finding.kind: p.steps for p in the_plan.planned}
    assert steps[MismatchKind.LOCAL_ONLY_ORDER] == (RecoverFills(recovery=recovery),)
    assert steps[MismatchKind.POSITION_MISMATCH] == (MarkUntrusted(instrument_id=BTC), CloseResidual(instrument_id=BTC))
    assert not the_plan.halts


def _fill(venue="V-1", qty="0.001"):
    return FillView(
        trade_id="T-1",
        instrument_id=BTC,
        venue_order_id=venue,
        client_order_id=None,
        side=OrderSide.BUY,
        quantity=Decimal(qty),
        price=Decimal("60000"),
        ts_event=1,
    )
