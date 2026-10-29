"""covers: spec 0023 AC-7 to AC-13 (a live check acts on its findings in the fixed order and reports each action),
AC-9 (the replacement stop is accepted before any old one is cancelled; refused, it flattens and halts; a crossed
trigger closes at market; no fresh quote waits, then flattens and halts), AC-23 (demo never acts), AC-27 (the quiet
compare and the confirming fetch), AC-28 (while flattening or halted only unknown entries are cancelled), and the
cache side of AC-6 (``local_view``) and AC-8, AC-11 (``ExecEngineReports``).

Same harness as ``test_node_reconcile``: the actor sits in a BacktestEngine for its clock and cache and
is driven directly with fakes. Needs the ``node`` extra.
"""

import asyncio
import logging
import uuid
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.identifiers import ClientOrderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_node_placeholder import INSTRUMENT
from test_node_reconcile import BTC, EMPTY, FOREIGN, FakeExchange, _actor, _rows
from test_reconcile_compare import OURS, TRIGGER, _held, _local, _order, _position, _stop

from trading_core.control.halt import HaltReason
from trading_core.node.reconcile import (
    NOT_QUIET,
    Actions,
    ExecEngineReports,
    KrakenExchangeView,
    ReconcileActionError,
    is_generated,
    local_view,
)
from trading_core.reconcile.views import ExchangeUnavailable, ExchangeView, PositionView
from trading_core.recording.enums import (
    ActionResult,
    MismatchAction,
    MismatchKind,
    PositionSide,
    ReconcileOutcome,
    ReconcileTrigger,
)
from trading_core.recording.rows import ReconciliationMismatchRow, ReconciliationRow
from trading_core.strategy.blocks import BlockReason


class Journal:
    """Every action any fake takes, in order."""

    def __init__(self):
        self.calls = []


class FakeExecutor:
    def __init__(self, journal, accept=True):
        self.journal, self.accept = journal, accept

    async def place_stop(self, step):
        self.journal.calls.append(("place_stop", step.quantity, step.trigger_price))
        return self.accept

    def close(self, position_id):
        self.journal.calls.append(("close", position_id))


class FakeCancel:
    def __init__(self, journal, fail=()):
        self.journal, self.fail = journal, set(fail)

    async def cancel(self, order):
        self.journal.calls.append(("cancel", order.client_order_id))
        if order.client_order_id in self.fail:
            raise ExchangeUnavailable("cancel rejected")


class FakeReportSink:
    def __init__(self, journal):
        self.journal = journal

    def mark_canceled(self, order):
        self.journal.calls.append(("mark_canceled", order.client_order_id))

    def sync_position(self, instrument_id, exchange):
        self.journal.calls.append(("sync_position", instrument_id, exchange))


class FakeKillSwitch:
    def __init__(self, journal, flattening=False, halted=False):
        self.journal, self.flattening, self.halted = journal, flattening, halted

    def halt_automatic(self, reason, *, flatten=True):
        self.journal.calls.append(("halt", reason, flatten))
        return uuid.uuid7()


def _acting(exchange, local, *, accept=True, fail=(), flattening=False, halted=False, **kwargs):
    journal = Journal()
    actions = Actions(
        executor=FakeExecutor(journal, accept),
        cancel=FakeCancel(journal, fail),
        reports=FakeReportSink(journal),
        kill_switch=FakeKillSwitch(journal, flattening, halted),
    )
    kwargs.setdefault("stale_quote_s", 0.05)
    actor, blocks, records = _actor(exchange, local=local, actions=actions, quiet_wait_s=0, **kwargs)
    return actor, blocks, records, journal


def _quote(actor, mid):
    half = Decimal(1)
    actor.cache.add_quote_tick(
        QuoteTick(
            instrument_id=INSTRUMENT.id,
            bid_price=Price(Decimal(mid) - half, 0),
            ask_price=Price(Decimal(mid) + half, 0),
            bid_size=Quantity.from_str("1.0000"),
            ask_size=Quantity.from_str("1.0000"),
            ts_event=actor.clock.timestamp_ns(),
            ts_init=actor.clock.timestamp_ns(),
        )
    )


def _view(orders=(), positions=()):
    return ExchangeView(orders=tuple(orders), positions=tuple(positions), fetched_ns=1)


def _mismatches(records):
    return _rows(records, ReconciliationMismatchRow)


def _check(actor, trigger=ReconcileTrigger.PERIODIC):
    return asyncio.run(actor.check(trigger))


# What a live check does -----------------------------------------------------------------------


def test_an_unknown_order_is_cancelled_and_its_row_says_so():
    """AC-10, AC-13."""
    actor, blocks, records, journal = _acting(FakeExchange(_view([FOREIGN])), _local())

    result = _check(actor)

    assert result.outcome is ReconcileOutcome.MISMATCH
    assert journal.calls == [("cancel", "drill-foreign-1")]
    (row,) = _mismatches(records)
    assert (row.kind, row.action, row.action_result) == (
        MismatchKind.UNKNOWN_ORDER,
        MismatchAction.CANCELLED,
        ActionResult.OK,
    )
    assert BlockReason.RECONCILING in blocks.active()  # entries resume on the next clean check


def test_a_cancel_kraken_refuses_is_reported_failed():
    actor, _, records, _ = _acting(FakeExchange(_view([FOREIGN])), _local(), fail={"drill-foreign-1"})

    _check(actor)

    (row,) = _mismatches(records)
    assert (row.action, row.action_result) == (MismatchAction.CANCELLED, ActionResult.FAILED)
    assert row.details["error"].startswith("ExchangeUnavailable")


def test_actions_run_in_the_fixed_order_then_the_halt():
    """Cancel unknown orders, mark local only orders, align the cache, then halt and flatten."""
    eth = _position("PF_ETHUSD.KRAKEN")
    local_only = _order("L-1", "VL-1")
    actor, _, records, journal = _acting(FakeExchange(_view([FOREIGN], [eth])), _local([local_only]))

    _check(actor)

    assert journal.calls == [
        ("cancel", "drill-foreign-1"),
        ("mark_canceled", "L-1"),
        ("sync_position", "PF_ETHUSD.KRAKEN", eth),
        ("halt", HaltReason.RECONCILE_MISMATCH, True),
    ]
    by_kind = {r.kind: r for r in _mismatches(records)}
    assert by_kind[MismatchKind.UNKNOWN_POSITION].action is MismatchAction.FLATTENED
    assert by_kind[MismatchKind.UNKNOWN_POSITION].action_result is ActionResult.PENDING  # the kill switch closes it
    assert by_kind[MismatchKind.UNKNOWN_POSITION].details["cache_fix"] == "synced"


def test_a_cache_position_kraken_lacks_halts_without_a_flatten():
    """AC-8, with the cache fix owed to /architect (Nautilus 1.231.0 cannot shrink a strategy's position)."""
    actor, _, records, journal = _acting(FakeExchange(EMPTY), _local(positions=[_held()]))

    _check(actor)

    assert journal.calls == [("halt", HaltReason.RECONCILE_MISMATCH, False)]
    (row,) = _mismatches(records)
    assert (row.kind, row.action, row.details["cache_fix"]) == (
        MismatchKind.POSITION_MISMATCH,
        MismatchAction.NONE,
        "owed",
    )


def test_a_missing_stop_is_placed_and_nothing_halts():
    """AC-9."""
    actor, _, records, journal = _acting(FakeExchange(_view(positions=[_position()])), _local(positions=[_held()]))
    _quote(actor, 65000)

    _check(actor)

    assert journal.calls == [("place_stop", Decimal("0.001"), TRIGGER)]
    (row,) = _mismatches(records)
    assert (row.kind, row.action, row.action_result) == (
        MismatchKind.MISSING_STOP,
        MismatchAction.STOP_REPLACED,
        ActionResult.OK,
    )
    assert row.details["trigger_price"] == str(TRIGGER)


def test_the_old_stop_is_cancelled_only_after_the_new_one_is_accepted():
    """AC-9: never naked."""
    half = _stop(qty="0.0005")
    actor, _, _, journal = _acting(FakeExchange(_view([half], [_position()])), _local([half], [_held()]))
    _quote(actor, 65000)

    _check(actor)

    assert journal.calls == [("place_stop", Decimal("0.001"), TRIGGER), ("cancel", "S-1")]


def test_a_refused_replacement_keeps_the_old_stop_and_flattens_and_halts():
    """AC-9."""
    half = _stop(qty="0.0005")
    actor, _, records, journal = _acting(
        FakeExchange(_view([half], [_position()])), _local([half], [_held()]), accept=False
    )
    _quote(actor, 65000)

    _check(actor)

    assert journal.calls == [
        ("place_stop", Decimal("0.001"), TRIGGER),
        ("halt", HaltReason.RECONCILE_MISMATCH, True),
    ]
    (row,) = _mismatches(records)
    assert (row.action, row.details["why"]) == (MismatchAction.FLATTENED, "stop_refused")


def test_a_crossed_trigger_closes_at_market_without_a_halt():
    """AC-9."""
    actor, _, records, journal = _acting(FakeExchange(_view(positions=[_position()])), _local(positions=[_held()]))
    _quote(actor, 59000)

    _check(actor)

    assert journal.calls == [("close", f"{BTC}-{OURS}")]
    (row,) = _mismatches(records)
    assert (row.action, row.action_result) == (MismatchAction.CLOSED_AT_MARKET, ActionResult.PENDING)


def test_without_a_fresh_quote_it_waits_then_flattens_and_halts():
    """AC-9."""
    actor, _, records, journal = _acting(FakeExchange(_view(positions=[_position()])), _local(positions=[_held()]))

    _check(actor)

    assert journal.calls == [("halt", HaltReason.RECONCILE_MISMATCH, True)]
    (row,) = _mismatches(records)
    assert row.details["why"] == "no_fresh_quote"


def test_a_quote_that_arrives_while_it_waits_is_used():
    actor, _, _, journal = _acting(
        FakeExchange(_view(positions=[_position()])), _local(positions=[_held()]), stale_quote_s=1.0
    )

    async def run():
        async def quote_soon():
            await asyncio.sleep(0.1)
            _quote(actor, 65000)

        task = asyncio.create_task(quote_soon())
        await actor.check(ReconcileTrigger.PERIODIC)
        await task

    asyncio.run(run())

    assert journal.calls == [("place_stop", Decimal("0.001"), TRIGGER)]


# Report only (AC-23, AC-28) -------------------------------------------------------------------


def test_while_halted_a_check_only_cancels_unknown_entries():
    """AC-28: no stop placed, nothing closed, no second halt."""
    foreign_stop = _stop("F-2", "VF-2")
    exchange = FakeExchange(_view([FOREIGN, foreign_stop], [_position()]))
    actor, _, records, journal = _acting(exchange, _local(positions=[_held()]), halted=True)
    _quote(actor, 65000)

    _check(actor)

    assert journal.calls == [("cancel", "drill-foreign-1")]
    actions = {r.client_order_id or str(r.kind): r.action for r in _mismatches(records)}
    assert actions["drill-foreign-1"] is MismatchAction.CANCELLED
    assert actions["F-2"] is MismatchAction.NONE
    assert actions["missing_stop"] is MismatchAction.NONE


def test_while_the_kill_switch_flattens_a_check_only_reports():
    actor, _, _, journal = _acting(FakeExchange(_view(positions=[_position()])), _local(), flattening=True)

    _check(actor)

    assert journal.calls == []


def test_demo_findings_are_never_acted_on_and_entries_are_not_blocked_by_them():
    """AC-23: the default actor has no actions."""
    actor, blocks, records = _actor(FakeExchange(_view([FOREIGN], [_position()])), local=_local())
    blocks.clear(BlockReason.RECONCILING)

    _check(actor)

    assert all(r.action is MismatchAction.NONE for r in _mismatches(records))
    assert blocks.active() == ()


# The quiet compare (AC-27) --------------------------------------------------------------------


class EventfulExchange(FakeExchange):
    """Delivers an order event to the actor during the first ``noisy`` fetches."""

    def __init__(self, *items, noisy):
        super().__init__(*items)
        self.noisy = noisy
        self.actor = None

    async def fetch(self):
        view = await super().fetch()
        if self.fetches <= self.noisy:
            assert self.actor is not None
            self.actor.note_order_event(object())
        return view


def test_a_fill_during_the_fetch_makes_it_fetch_again_and_gives_no_false_finding():
    stale, fresh = _view([FOREIGN]), EMPTY
    exchange = EventfulExchange(stale, fresh, noisy=1)
    actor, _, _, journal = _acting(exchange, _local())
    exchange.actor = actor

    result = _check(actor)

    assert result.outcome is ReconcileOutcome.CLEAN
    assert journal.calls == []
    assert exchange.fetches == 2


def test_three_busy_tries_end_failed_not_quiet():
    exchange = EventfulExchange(EMPTY, noisy=99)
    actor, blocks, records, _ = _acting(exchange, _local())
    exchange.actor = actor

    result = _check(actor, ReconcileTrigger.START)

    assert (result.outcome, result.error) == (ReconcileOutcome.FAILED, NOT_QUIET)
    assert exchange.fetches == 3
    (row,) = _rows(records, ReconciliationRow)
    assert (row.outcome, row.error) == (ReconcileOutcome.FAILED, NOT_QUIET)
    assert BlockReason.EXCHANGE_UNKNOWN not in blocks.active()  # Kraken answered; it was only busy


def test_a_finding_the_second_fetch_does_not_confirm_is_not_acted_on():
    actor, _, records, journal = _acting(FakeExchange(_view([FOREIGN]), EMPTY), _local())

    result = _check(actor)

    assert result.outcome is ReconcileOutcome.CLEAN
    assert journal.calls == []
    assert _mismatches(records) == []


def test_a_failed_confirming_fetch_fails_the_check():
    actor, blocks, _, journal = _acting(FakeExchange(_view([FOREIGN]), ExchangeUnavailable("down")), _local())

    result = _check(actor)

    assert result.outcome is ReconcileOutcome.FAILED
    assert journal.calls == []
    assert BlockReason.EXCHANGE_UNKNOWN in blocks.active()


def test_a_critical_line_names_the_action_taken(caplog):
    actor, _, _, _ = _acting(FakeExchange(_view([FOREIGN])), _local())

    with caplog.at_level(logging.CRITICAL):
        _check(actor)

    (line,) = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "reconcile_mismatch"]
    assert (line.fields["action"], line.fields["action_result"], line.fields["halts"]) == ("cancelled", "ok", False)


# The cache side -------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, ok=True):
        self.reports, self.ok = [], ok

    def reconcile_execution_report(self, report):
        self.reports.append(report)
        return self.ok


def test_sync_position_feeds_kraken_side_and_size_to_nautilus():
    actor, _, _ = _actor(FakeExchange(EMPTY))
    engine = FakeEngine()
    sink = ExecEngineReports(engine, actor.cache, now_ns=lambda: 5)

    sink.sync_position(BTC, PositionView(instrument_id=BTC, side=PositionSide.SHORT, quantity=Decimal("0.0003")))
    sink.sync_position(BTC, None)

    short, flat = engine.reports
    assert (short.position_side, short.quantity, short.ts_last) == (
        NautilusPositionSide.SHORT,
        Quantity.from_str("0.0003"),
        5,
    )
    assert (flat.position_side, flat.quantity.as_decimal()) == (NautilusPositionSide.FLAT, Decimal(0))


def test_a_report_nautilus_refuses_raises():
    actor, _, _ = _actor(FakeExchange(EMPTY))
    sink = ExecEngineReports(FakeEngine(ok=False), actor.cache)

    with pytest.raises(ReconcileActionError):
        sink.sync_position(BTC, None)


def test_mark_canceled_needs_the_order_in_the_cache():
    actor, _, _ = _actor(FakeExchange(EMPTY))
    sink = ExecEngineReports(FakeEngine(), actor.cache)

    with pytest.raises(ReconcileActionError):
        sink.mark_canceled(_order("not-cached"))


def test_mark_canceled_reports_the_cached_order_canceled():
    actor, _, _ = _actor(FakeExchange(EMPTY))
    order = TestExecStubs.limit_order(instrument=INSTRUMENT)
    actor.cache.add_order(order)
    engine = FakeEngine()

    ExecEngineReports(engine, actor.cache, now_ns=lambda: 9).mark_canceled(_order(str(order.client_order_id)))

    (report,) = engine.reports
    assert report.order_status is OrderStatus.CANCELED
    assert report.client_order_id == order.client_order_id
    assert str(report.venue_order_id).startswith("RECON-")  # never acknowledged


class CancelReports:
    def __init__(self, error=None):
        self.error, self.cancelled = error, []

    async def order_reports(self):
        return []

    async def position_reports(self):
        return []

    async def cancel(self, order):
        if self.error is not None:
            raise self.error
        self.cancelled.append(order)


def test_kraken_cancel_raises_unless_kraken_confirmed():
    with pytest.raises(ExchangeUnavailable):
        asyncio.run(KrakenExchangeView(CancelReports(RuntimeError("HTTP 400")), 1.0).cancel(FOREIGN))

    reports = CancelReports()
    asyncio.run(KrakenExchangeView(reports, 1.0).cancel(FOREIGN))
    assert reports.cancelled == [FOREIGN]


def _cache_open(actor, order):
    actor.cache.add_order(order)
    actor.cache.update_order(order)  # indexes it as open


def test_local_view_keeps_only_our_open_orders_never_generated_ones():
    """AC-6: an order Nautilus generated (``EXTERNAL``, or tagged ``VENUE``) is never ours, so Kraken's copy of
    it is an ``unknown_order``."""
    actor, _, _ = _actor(FakeExchange(EMPTY))
    ours = TestExecStubs.make_accepted_order(
        TestExecStubs.limit_order(
            instrument=INSTRUMENT, strategy_id=StrategyId(OURS), client_order_id=ClientOrderId("O-1")
        )
    )
    external = TestExecStubs.make_accepted_order(
        TestExecStubs.limit_order(
            instrument=INSTRUMENT, strategy_id=StrategyId("EXTERNAL"), client_order_id=ClientOrderId("X-1")
        )
    )
    venue = TestExecStubs.make_accepted_order(
        TestExecStubs.limit_order(
            instrument=INSTRUMENT, strategy_id=StrategyId(OURS), client_order_id=ClientOrderId("V-1"), tags=["VENUE"]
        )
    )
    for order in (ours, external, venue):
        _cache_open(actor, order)

    view = local_view(actor.cache, frozenset({OURS}))

    assert [o.client_order_id for o in view.orders] == ["O-1"]
    assert view.orders[0].strategy_id == OURS
    assert view.owned_strategies == frozenset({OURS})
    assert is_generated(external)
    assert is_generated(venue)
    assert not is_generated(ours)


def test_unowned_strategies_see_no_orders_at_all():
    actor, _, _ = _actor(FakeExchange(EMPTY))
    order = TestExecStubs.make_accepted_order(
        TestExecStubs.limit_order(instrument=INSTRUMENT, strategy_id=StrategyId(OURS))
    )
    _cache_open(actor, order)

    assert local_view(actor.cache).orders == ()
