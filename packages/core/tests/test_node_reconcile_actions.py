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

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import LiquiditySide, OrderStatus
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, StrategyId, TradeId, VenueOrderId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from test_node_placeholder import INSTRUMENT
from test_node_reconcile import BTC, EMPTY, FOREIGN, FakeExchange, _actor, _rows
from test_reconcile_compare import OURS, TRIGGER, _held, _local, _order, _position, _stop

from trading_core.control.halt import HaltReason
from trading_core.node.reconcile import (
    NOT_QUIET,
    RECOVERY_INCOMPLETE,
    Actions,
    ExecEngineReports,
    KrakenExchangeView,
    ReconcileActionError,
    is_generated,
    local_view,
)
from trading_core.reconcile.recover import KRAKEN_FILLS_PAGE
from trading_core.reconcile.views import ExchangeUnavailable, ExchangeView, FillView, PositionView
from trading_core.recording.enums import (
    ActionResult,
    MismatchAction,
    MismatchKind,
    OrderSide,
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

    def close_residual(self, instrument_id):
        self.journal.calls.append(("close_residual", instrument_id))


class FakeCancel:
    def __init__(self, journal, fail=()):
        self.journal, self.fail = journal, set(fail)

    async def cancel(self, order):
        self.journal.calls.append(("cancel", order.client_order_id))
        if order.client_order_id in self.fail:
            raise ExchangeUnavailable("cancel rejected")


class FakeReportSink:
    def __init__(self, journal):
        self.journal, self.recovered = journal, []

    def mark_canceled(self, order):
        self.journal.calls.append(("mark_canceled", order.client_order_id))

    def sync_position(self, instrument_id, exchange):
        self.journal.calls.append(("sync_position", instrument_id, exchange))

    def recover(self, recovery):
        self.journal.calls.append(("recover", recovery.order.client_order_id, [f.trade_id for f in recovery.fills]))
        self.recovered.append(recovery)


class FakeFills:
    """Hands out the given fills, or raises the given error."""

    def __init__(self, fills=()):
        self.fills_or_error, self.since = fills, []

    async def fills(self, since_ns):
        self.since.append(since_ns)
        if isinstance(self.fills_or_error, Exception):
            raise self.fills_or_error
        return tuple(self.fills_or_error)


class FakeUntrusted:
    def __init__(self, journal, ok=True):
        self.journal, self.ok, self.instruments = journal, ok, set()

    async def add(self, instrument_id):
        self.journal.calls.append(("untrusted_add", instrument_id))
        self.instruments.add(instrument_id)
        return self.ok

    async def clear(self):
        self.journal.calls.append(("untrusted_clear",))
        self.instruments.clear()
        return True


class FakeKillSwitch:
    def __init__(self, journal, flattening=False, halted=False):
        self.journal, self.flattening, self.halted = journal, flattening, halted

    def halt_automatic(self, reason, *, flatten=True):
        self.journal.calls.append(("halt", reason, flatten))
        return uuid.uuid7()


def _acting(
    exchange,
    local,
    *,
    accept=True,
    fail=(),
    flattening=False,
    halted=False,
    fills=(),
    untrusted_ok=True,
    sink=None,
    journal=None,
    **kwargs,
):
    journal = journal or Journal()
    actions = Actions(
        executor=FakeExecutor(journal, accept),
        cancel=FakeCancel(journal, fail),
        reports=sink or FakeReportSink(journal),
        kill_switch=FakeKillSwitch(journal, flattening, halted),
        fills=fills if isinstance(fills, FakeFills) else FakeFills(fills),
        untrusted=FakeUntrusted(journal, untrusted_ok),
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


def test_a_cache_position_kraken_lacks_marks_it_untrusted_closes_kraken_and_halts():
    """AC-8: Nautilus 1.231.0 cannot shrink a strategy's position, so the cache waits for a hand reset."""
    actor, _, records, journal = _acting(FakeExchange(EMPTY), _local(positions=[_held()]))

    _check(actor)

    assert journal.calls == [
        ("untrusted_add", BTC),
        ("close_residual", BTC),
        ("halt", HaltReason.RECONCILE_MISMATCH, True),
    ]
    (row,) = _mismatches(records)
    assert (row.kind, row.action, row.action_result, row.details["cache_fix"]) == (
        MismatchKind.POSITION_MISMATCH,
        MismatchAction.NONE,
        ActionResult.PENDING,
        "manual",
    )


def test_a_cache_larger_than_kraken_closes_only_kraken_s_position():
    """AC-8: the executor closes what Kraken holds, the action says flattened."""
    actor, _, records, journal = _acting(
        FakeExchange(_view(positions=[_position(qty="0.001")])), _local(positions=[_held(qty="0.003")])
    )

    _check(actor)

    assert ("close_residual", BTC) in journal.calls
    (row,) = _mismatches(records)
    assert (row.action, row.details["cache_fix"]) == (MismatchAction.FLATTENED, "manual")


def test_an_untrusted_set_redis_refused_reports_the_finding_failed():
    """Fail closed: the skip still holds in memory, but the row says the write failed."""
    actor, _, records, journal = _acting(FakeExchange(EMPTY), _local(positions=[_held()]), untrusted_ok=False)

    _check(actor)

    (row,) = _mismatches(records)
    assert row.action_result is ActionResult.FAILED
    assert "untrusted" in row.details["error"]
    assert ("halt", HaltReason.RECONCILE_MISMATCH, True) in journal.calls


def test_while_halted_a_cache_larger_than_kraken_still_closes_kraken_but_never_halts_again():
    """AC-28."""
    actor, _, _, journal = _acting(FakeExchange(EMPTY), _local(positions=[_held()]), halted=True)

    _check(actor)

    assert journal.calls == [("untrusted_add", BTC), ("close_residual", BTC)]


def test_a_clean_start_check_empties_the_untrusted_set():
    """AC-8."""
    actor, _, _, journal = _acting(FakeExchange(EMPTY), _local())

    _check(actor, ReconcileTrigger.START)

    assert journal.calls == [("untrusted_clear",)]


def test_a_clean_periodic_check_keeps_the_untrusted_set():
    actor, _, _, journal = _acting(FakeExchange(EMPTY), _local())

    _check(actor)

    assert journal.calls == []


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


# Fill recovery (AC-11) ------------------------------------------------------------------------


def _fill(trade="T-1", venue="VS-1", client="S-1", qty="0.001", side=OrderSide.SELL, ts=1):
    return FillView(
        trade_id=trade,
        instrument_id=BTC,
        venue_order_id=venue,
        client_order_id=client,
        side=side,
        quantity=Decimal(qty),
        price=Decimal("60000"),
        ts_event=ts,
    )


def _recovering(exchange, before, after, **kwargs):
    """An actor whose cache reads ``before`` until the sink recovered something, then ``after``."""
    journal = Journal()
    sink = FakeReportSink(journal)
    actor, blocks, records, _ = _acting(
        exchange, lambda _cache: after if sink.recovered else before, sink=sink, journal=journal, **kwargs
    )
    return actor, blocks, records, journal


def test_a_missed_stop_fill_is_recovered_and_nothing_halts():
    """AC-11: the stop filled while the trader was down; the compare after it finds the cache flat too."""
    stop = _stop()
    actor, blocks, records, journal = _recovering(
        FakeExchange(EMPTY), _local([stop], [_held()]), _local(), fills=[_fill(), _fill("T-other", "V-other")]
    )

    result = _check(actor)

    assert result.outcome is ReconcileOutcome.MISMATCH
    assert journal.calls == [("recover", "S-1", ["T-1"])]
    (row,) = _mismatches(records)
    assert (row.kind, row.action, row.action_result) == (
        MismatchKind.LOCAL_ONLY_ORDER,
        MismatchAction.ADOPTED,
        ActionResult.OK,
    )
    assert (row.details["cache_fix"], row.details["order_status"], row.details["fills"]) == ("recovered", "filled", "1")
    assert BlockReason.RECONCILING in blocks.active()  # entries wait for the next clean check
    (check_row,) = _rows(records, ReconciliationRow)
    assert check_row.mismatch_count == 1


def test_a_partly_filled_local_only_order_is_recovered_canceled():
    stop = _stop()
    actor, _, records, _ = _recovering(
        FakeExchange(_view(positions=[_position(qty="0.0005")])),
        _local([stop], [_held()]),
        _local(positions=[_held(qty="0.0005")]),
        fills=[_fill(qty="0.0005")],
    )
    _quote(actor, 65000)

    _check(actor)

    by_kind = {r.kind: r for r in _mismatches(records)}
    assert by_kind[MismatchKind.LOCAL_ONLY_ORDER].details["order_status"] == "canceled"
    assert by_kind[MismatchKind.LOCAL_ONLY_ORDER].details["filled_qty"] == "0.0005"


def test_a_local_only_order_with_no_fill_is_marked_canceled_without_a_second_compare():
    exchange = FakeExchange(EMPTY)
    actor, _, records, journal = _acting(exchange, _local([_order("L-1", "VL-1")]), fills=[_fill()])

    _check(actor)

    assert journal.calls == [("mark_canceled", "L-1")]
    assert exchange.fetches == 2  # the compare and its confirming fetch, no recovery pass
    (row,) = _mismatches(records)
    assert row.action is MismatchAction.MARKED_CANCELED


def test_a_fill_fetch_error_fails_the_check_exchange_unknown():
    """AC-11, AC-4: an error is never "no fills"."""
    actor, blocks, _, journal = _acting(
        FakeExchange(EMPTY), _local([_order("L-1", "VL-1")]), fills=ExchangeUnavailable("fills timed out")
    )

    result = _check(actor)

    assert (result.outcome, result.error) == (ReconcileOutcome.FAILED, "fills timed out")
    assert journal.calls == []
    assert BlockReason.EXCHANGE_UNKNOWN in blocks.active()


def test_a_second_pass_that_still_finds_orders_to_recover_fails_the_check():
    """AC-11: only one recovery pass per check."""
    stop = _stop()
    actor, blocks, records, journal = _acting(FakeExchange(EMPTY), _local([stop], [_held()]), fills=[_fill()])

    result = _check(actor)

    assert (result.outcome, result.error) == (ReconcileOutcome.FAILED, RECOVERY_INCOMPLETE)
    assert journal.calls == [("recover", "S-1", ["T-1"])]  # no halt, no untrusted set
    (row,) = _mismatches(records)
    assert row.action is MismatchAction.ADOPTED
    assert BlockReason.RECONCILING in blocks.active()


def test_recovery_that_leaves_the_cache_below_kraken_takes_the_synced_path():
    """AC-8: the cache holding less than Kraken after recovery is AC-7's case."""
    entry = _order("E-2", "VE-2", side=OrderSide.BUY)
    kraken = _position(qty="0.003")
    actor, _, records, journal = _recovering(
        FakeExchange(_view(positions=[kraken])),
        _local([entry], [_held()]),
        _local(positions=[_held(qty="0.002")]),
        fills=[_fill("T-2", "VE-2", "E-2", side=OrderSide.BUY)],
    )

    _check(actor)

    assert journal.calls == [
        ("recover", "E-2", ["T-2"]),
        ("sync_position", BTC, kraken),
        ("halt", HaltReason.RECONCILE_MISMATCH, True),
    ]
    by_kind = {r.kind: r for r in _mismatches(records)}
    assert by_kind[MismatchKind.POSITION_MISMATCH].details["cache_fix"] == "synced"


def test_while_halted_missed_fills_are_still_recovered():
    """AC-28: recovery only corrects the books."""
    actor, _, _, journal = _recovering(
        FakeExchange(EMPTY), _local([_stop()], [_held()]), _local(), fills=[_fill()], halted=True
    )

    _check(actor)

    assert journal.calls == [("recover", "S-1", ["T-1"])]


def test_fills_are_fetched_over_the_lookback():
    fills = FakeFills([_fill()])
    actor, _, _, _ = _recovering(
        FakeExchange(EMPTY), _local([_stop()], [_held()]), _local(), fills=fills, lookback_mins=10
    )

    _check(actor)

    assert fills.since == [actor.clock.timestamp_ns() - 10 * 60 * 1_000_000_000]


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

    async def fill_reports(self):
        return []

    async def cancel(self, order):
        if self.error is not None:
            raise self.error
        self.cancelled.append(order)


class FillReports(CancelReports):
    def __init__(self, fills=(), error=None):
        super().__init__(error)
        self.fills = list(fills)

    async def fill_reports(self):
        if self.error is not None:
            raise self.error
        return list(self.fills)


def _fill_report(trade="T-1", ts=100, client=None):
    return FillReport(
        account_id=AccountId("KRAKEN-UNIFIED"),
        instrument_id=INSTRUMENT.id,
        venue_order_id=VenueOrderId("V-1"),
        trade_id=TradeId(trade),
        order_side=NautilusOrderSide.SELL,
        last_qty=Quantity.from_str("0.0010"),
        last_px=Price.from_str("60123.5"),
        commission=Money(Decimal("0.03"), USD),
        liquidity_side=LiquiditySide.TAKER,
        report_id=UUID4(),
        ts_event=ts,
        ts_init=ts,
        client_order_id=None if client is None else ClientOrderId(client),
    )


def test_kraken_fills_come_as_kraken_returned_them_inside_the_window():
    """AC-11: price, venue time, trade id, commission; fills before the window are dropped."""
    reports = FillReports([_fill_report("T-1", ts=100, client="S-1"), _fill_report("T-0", ts=10)])

    (fill,) = asyncio.run(KrakenExchangeView(reports, 1.0).fills(since_ns=50))

    assert (fill.trade_id, fill.venue_order_id, fill.client_order_id, fill.side) == (
        "T-1",
        "V-1",
        "S-1",
        OrderSide.SELL,
    )
    assert (fill.quantity, fill.price, fill.ts_event) == (Decimal("0.0010"), Decimal("60123.5"), 100)
    assert (fill.commission, fill.commission_currency, fill.liquidity_side) == (Decimal("0.03"), "USD", "taker")


def test_a_kraken_fill_fetch_error_is_never_no_fills():
    with pytest.raises(ExchangeUnavailable):
        asyncio.run(KrakenExchangeView(FillReports(error=RuntimeError("HTTP 503")), 1.0).fills(since_ns=0))


def test_a_full_page_of_kraken_fills_inside_the_window_is_refused():
    """AC-11: Kraken returns its newest 100 fills and the adapter cannot page, so fills may be missing."""
    reports = FillReports([_fill_report(f"T-{i}", ts=100 + i) for i in range(KRAKEN_FILLS_PAGE)])

    with pytest.raises(ExchangeUnavailable, match="full page"):
        asyncio.run(KrakenExchangeView(reports, 1.0).fills(since_ns=50))


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
