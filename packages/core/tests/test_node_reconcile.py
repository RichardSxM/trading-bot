"""covers: spec 0023 AC-1 (the start check and the ``reconciling`` block), AC-2 (the periodic check), AC-3 (the
reconnect check), AC-4 (a failed fetch is ``failed`` and ``exchange_unknown``, retried with backoff, and Kraken's
view fails closed), AC-5 (the start fetch and the watchdog), AC-13 (each finding's row, risk event, and CRITICAL log),
AC-17 (which checks write a row), AC-20 (recheck), AC-23 (demo only reports).

The actor is registered with a BacktestEngine for its clock and cache, then driven directly with
a fake exchange port inside ``asyncio.run``. Needs the ``node`` extra.
"""

import asyncio
import itertools
import logging
import uuid
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderStatus, OrderType, TimeInForce, TriggerType
from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, Venue, VenueOrderId
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_placeholder import INSTRUMENT

from trading_core.node.reconcile import (
    EVENT_KEY_PREFIX,
    TOUCH_EVERY_S,
    KrakenExchangeView,
    ReconcileActor,
    wait_for_exchange,
    watch_start,
)
from trading_core.reconcile.views import ExchangeUnavailable, ExchangeView, LocalView, OrderView
from trading_core.recording.enums import (
    ControlOutcome,
    Env,
    MismatchAction,
    MismatchKind,
    OrderSide,
    PositionSide,
    ReconcileOutcome,
    ReconcileTrigger,
    RiskEventKind,
    Severity,
)
from trading_core.recording.rows import ReconciliationMismatchRow, ReconciliationRow, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks

BTC = str(INSTRUMENT.id)
EMPTY = ExchangeView(orders=(), positions=(), fetched_ns=1)
FOREIGN = OrderView(
    instrument_id=BTC,
    client_order_id="drill-foreign-1",
    venue_order_id="V-7",
    side=OrderSide.SELL,
    order_type="limit",
    quantity=Decimal("0.0001"),
)


class FakeExchange:
    """Hands out the given views (or raises the given errors) in turn; the last one repeats."""

    def __init__(self, *items):
        self.items = list(items)
        self.fetches = 0

    async def fetch(self):
        self.fetches += 1
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def _actor(exchange, *, record=True, local=None, **kwargs):
    if local is not None:
        kwargs["local"] = local if callable(local) else (lambda _cache: local)
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(1000, USD)])
    engine.add_instrument(INSTRUMENT)
    blocks = EntryBlocks()
    blocks.add(BlockReason.RECONCILING)  # on_start's job; it needs the live node's event loop
    records: list[WriteItem] = []
    actor = ReconcileActor(
        env=Env.DEMO,
        exchange=exchange,
        blocks=blocks,
        record=records.append if record else None,
        run_id=uuid.uuid7() if record else None,
        **kwargs,
    )
    engine.add_actor(actor)
    return actor, blocks, records


def _rows(records, row_type):
    return [item.row for item in records if isinstance(item.row, row_type)]


def test_a_clean_start_check_lifts_reconciling_and_writes_one_row():
    actor, blocks, records = _actor(FakeExchange(EMPTY))

    result = asyncio.run(actor.check(ReconcileTrigger.START))

    assert result.outcome is ReconcileOutcome.CLEAN
    assert blocks.active() == ()
    (row,) = _rows(records, ReconciliationRow)
    assert row.id == result.reconciliation_id
    assert (row.trigger, row.outcome, row.mismatch_count) == (ReconcileTrigger.START, ReconcileOutcome.CLEAN, 0)
    assert (row.exchange_orders, row.exchange_positions) == (0, 0)
    assert row.finished_ns >= row.started_ns
    assert [item.call for item in records] == [WriteCall.RECORD_RECONCILIATION]
    fields = actor.snapshot_fields()
    assert fields == {"last_clean_check_ns": row.finished_ns, "last_check_outcome": "clean", "intent": {}}


def test_before_any_check_the_snapshot_fields_are_null():
    actor, _, _ = _actor(FakeExchange(EMPTY))

    assert actor.snapshot_fields() == {"last_clean_check_ns": None, "last_check_outcome": None, "intent": {}}


def test_a_clean_check_leaves_other_blocks_alone():
    actor, blocks, _ = _actor(FakeExchange(EMPTY))
    blocks.add(BlockReason.STALE_FEED)

    asyncio.run(actor.check(ReconcileTrigger.START))

    assert blocks.active() == (BlockReason.STALE_FEED,)


def test_a_mismatch_keeps_entries_blocked_and_reports_each_finding(caplog):
    actor, blocks, records = _actor(FakeExchange(ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1)))

    with caplog.at_level(logging.CRITICAL):
        result = asyncio.run(actor.check(ReconcileTrigger.START))

    assert result.outcome is ReconcileOutcome.MISMATCH
    assert blocks.active() == (BlockReason.RECONCILING,)
    (check,) = _rows(records, ReconciliationRow)
    assert (check.outcome, check.mismatch_count, check.exchange_orders) == (ReconcileOutcome.MISMATCH, 1, 1)
    (mismatch,) = _rows(records, ReconciliationMismatchRow)
    assert mismatch.reconciliation_id == check.id
    assert mismatch.kind is MismatchKind.UNKNOWN_ORDER
    assert (mismatch.client_order_id, mismatch.venue_order_id) == ("drill-foreign-1", "V-7")
    assert (mismatch.exchange_side, mismatch.exchange_qty) == ("sell", Decimal("0.0001"))
    assert mismatch.action is MismatchAction.NONE  # demo only reports (AC-23)
    (event,) = _rows(records, RiskEventRow)
    assert event.event_key == f"{EVENT_KEY_PREFIX}{mismatch.id}"
    assert (event.kind, event.severity) == (RiskEventKind.RECONCILE_MISMATCH, Severity.CRITICAL)
    # The parent row is queued before its children, so the writer inserts it first.
    assert [item.call for item in records] == [
        WriteCall.RECORD_RECONCILIATION,
        WriteCall.RECORD_RECONCILIATION_MISMATCH,
        WriteCall.RECORD_RISK_EVENT,
    ]
    (line,) = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "reconcile_mismatch"]
    assert line.levelno == logging.CRITICAL
    assert line.fields["event_key"] == event.event_key
    assert actor.snapshot_fields() == {
        "last_clean_check_ns": None,
        "last_check_outcome": "mismatch",
        "intent": {},
    }


def test_a_local_order_the_exchange_lacks_is_reported_from_the_local_view():
    local = LocalView(orders=(FOREIGN,), positions=())
    actor, _, records = _actor(FakeExchange(EMPTY), local=local)

    asyncio.run(actor.check(ReconcileTrigger.START))

    (mismatch,) = _rows(records, ReconciliationMismatchRow)
    assert mismatch.kind is MismatchKind.LOCAL_ONLY_ORDER


def test_a_failed_fetch_is_failed_never_an_empty_view_and_entries_stay_blocked():
    actor, blocks, records = _actor(FakeExchange(ExchangeUnavailable("HTTP 503")))

    result = asyncio.run(actor.check(ReconcileTrigger.START))

    assert (result.outcome, result.error) == (ReconcileOutcome.FAILED, "HTTP 503")
    assert blocks.active() == (BlockReason.EXCHANGE_UNKNOWN, BlockReason.RECONCILING)
    (row,) = _rows(records, ReconciliationRow)
    assert (row.outcome, row.error) == (ReconcileOutcome.FAILED, "HTTP 503")
    assert (row.exchange_orders, row.exchange_positions) == (None, None)
    assert actor.snapshot_fields()["last_check_outcome"] == "failed"


def test_a_clean_check_after_a_failed_one_lifts_the_block():
    actor, blocks, _ = _actor(FakeExchange(ExchangeUnavailable("timeout"), EMPTY))

    asyncio.run(actor.check(ReconcileTrigger.START))
    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))

    assert blocks.active() == ()


def test_a_clean_periodic_check_writes_no_row_but_moves_the_last_clean_time():
    actor, _, records = _actor(FakeExchange(EMPTY))

    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))

    assert records == []
    assert actor.snapshot_fields()["last_clean_check_ns"] is not None


def test_a_periodic_mismatch_and_a_reconnect_check_write_rows():
    mismatch = ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1)
    actor, _, records = _actor(FakeExchange(mismatch, mismatch, EMPTY))  # a finding is fetched twice (AC-27)

    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))
    asyncio.run(actor.check(ReconcileTrigger.RECONNECT))

    rows = _rows(records, ReconciliationRow)
    assert [(r.trigger, r.outcome) for r in rows] == [
        (ReconcileTrigger.PERIODIC, ReconcileOutcome.MISMATCH),
        (ReconcileTrigger.RECONNECT, ReconcileOutcome.CLEAN),
    ]


def test_without_a_run_checks_still_decide_but_write_nothing(caplog):
    actor, _, records = _actor(FakeExchange(ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1)), record=False)

    with caplog.at_level(logging.CRITICAL):
        result = asyncio.run(actor.check(ReconcileTrigger.START))

    assert result.outcome is ReconcileOutcome.MISMATCH
    assert records == []
    assert any(getattr(r, "fields", {}).get("code") == "reconcile_mismatch" for r in caplog.records)


def test_checks_never_overlap_and_a_trigger_during_one_is_queued_once():
    gate = asyncio.Event

    class SlowExchange(FakeExchange):
        release: asyncio.Event

        async def fetch(self):
            await self.release.wait()
            return await super().fetch()

    exchange = SlowExchange(EMPTY)
    actor, _, records = _actor(exchange)

    async def run():
        exchange.release = gate()
        actor.request_check(ReconcileTrigger.START)
        actor.request_check(ReconcileTrigger.RECONNECT)
        actor.request_check(ReconcileTrigger.PERIODIC)  # dropped: one is already queued
        await asyncio.sleep(0)
        exchange.release.set()
        await actor._task

    asyncio.run(run())

    assert exchange.fetches == 2
    assert [r.trigger for r in _rows(records, ReconciliationRow)] == [
        ReconcileTrigger.START,
        ReconcileTrigger.RECONNECT,
    ]


def test_a_mismatch_after_a_failed_fetch_keeps_exchange_unknown_until_a_clean_check():
    mismatch = ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1)
    actor, blocks, _ = _actor(FakeExchange(ExchangeUnavailable("timeout"), mismatch, mismatch, EMPTY))

    asyncio.run(actor.check(ReconcileTrigger.START))
    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))
    assert BlockReason.EXCHANGE_UNKNOWN in blocks.active()
    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))
    assert blocks.active() == ()


# Scheduling (AC-2, AC-4) --------------------------------------------------------------------


def _delays(actor, runs):
    """Run each trigger through ``request_check`` and return the delay and trigger of the next check it scheduled."""

    async def run():
        seen = []
        for trigger in runs:
            actor.request_check(trigger)
            await actor._task
            timer = actor._timer
            seen.append((round(timer.when() - asyncio.get_running_loop().time()), timer._args[0]))
            timer.cancel()
        return seen

    return asyncio.run(run())


def test_a_finished_check_schedules_the_next_periodic_one_an_interval_later():
    actor, _, _ = _actor(FakeExchange(EMPTY), interval_s=60.0)

    assert _delays(actor, [ReconcileTrigger.START]) == [(60, ReconcileTrigger.PERIODIC)]


def test_a_failed_fetch_retries_the_same_trigger_with_a_doubling_backoff_up_to_60_s():
    actor, _, _ = _actor(FakeExchange(ExchangeUnavailable("HTTP 503")), retry_s=5.0)

    delays = _delays(actor, [ReconcileTrigger.START] * 6)

    assert [d for d, _ in delays] == [5, 10, 20, 40, 60, 60]
    assert {t for _, t in delays} == {ReconcileTrigger.START}


def test_a_clean_check_resets_the_backoff():
    actor, _, _ = _actor(FakeExchange(ExchangeUnavailable("x"), ExchangeUnavailable("x"), EMPTY), retry_s=5.0)

    _delays(actor, [ReconcileTrigger.START] * 3)

    assert actor._next_retry_s == 5.0


def test_a_requested_check_replaces_the_scheduled_one():
    actor, _, _ = _actor(FakeExchange(EMPTY))

    async def run():
        actor.request_check(ReconcileTrigger.START)
        await actor._task
        scheduled = actor._timer
        actor.request_check(ReconcileTrigger.PERIODIC)
        assert scheduled.cancelled()
        await actor._task
        actor._timer.cancel()

    asyncio.run(run())


def test_a_stopped_actor_schedules_and_runs_nothing():
    exchange = FakeExchange(EMPTY)
    actor, _, _ = _actor(exchange)

    async def run():
        actor.on_stop()
        actor.request_check(ReconcileTrigger.PERIODIC)
        assert actor._task is None
        return await actor.recheck()

    result = asyncio.run(run())

    assert (result.outcome, result.error) == (ReconcileOutcome.FAILED, "stopped")
    assert exchange.fetches == 0


# Reconnect (AC-3) ----------------------------------------------------------------------------


def test_a_stale_feed_blocks_reconciling_at_once_and_its_clearing_runs_a_reconnect_check():
    actor, blocks, records = _actor(FakeExchange(EMPTY))

    async def run():
        actor.on_start()
        await actor._task
        actor._timer.cancel()
        blocks.add(BlockReason.STALE_FEED)
        assert BlockReason.RECONCILING in blocks.active()
        blocks.clear(BlockReason.STALE_FEED)
        assert BlockReason.RECONCILING in blocks.active()  # until the reconnect check is clean
        await asyncio.sleep(0)  # the reconnect check is scheduled thread safely
        await actor._task
        actor._timer.cancel()

    asyncio.run(run())

    assert blocks.active() == ()
    assert [r.trigger for r in _rows(records, ReconciliationRow)] == [
        ReconcileTrigger.START,
        ReconcileTrigger.RECONNECT,
    ]


# Recheck (AC-20) ----------------------------------------------------------------------------


def test_recheck_runs_a_periodic_check_and_answers_executed_with_its_outcome():
    actor, _, _ = _actor(FakeExchange(EMPTY))

    async def run():
        outcome = await actor.recheck_outcome()
        actor._timer.cancel()
        return outcome

    outcome = asyncio.run(run())

    assert outcome.outcome is ControlOutcome.EXECUTED
    assert outcome.details["outcome"] == "clean"
    assert uuid.UUID(outcome.details["reconciliation_id"])


def test_a_recheck_that_cannot_reach_kraken_answers_failed_exchange_unknown():
    actor, _, _ = _actor(FakeExchange(ExchangeUnavailable("HTTP 503")))

    async def run():
        outcome = await actor.recheck_outcome()
        actor._timer.cancel()
        return outcome

    outcome = asyncio.run(run())

    assert (outcome.outcome, outcome.reason) == (ControlOutcome.FAILED, "exchange_unknown")
    assert (outcome.details["outcome"], outcome.details["error"]) == ("failed", "HTTP 503")


def test_a_recheck_during_a_check_gets_the_next_checks_result():
    class SlowExchange(FakeExchange):
        release: asyncio.Event

        async def fetch(self):
            await self.release.wait()
            return await super().fetch()

    exchange = SlowExchange(EMPTY, ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=2))
    actor, _, records = _actor(exchange)

    async def run():
        exchange.release = asyncio.Event()
        actor.request_check(ReconcileTrigger.START)
        await asyncio.sleep(0)
        recheck = asyncio.ensure_future(actor.recheck())
        await asyncio.sleep(0)
        exchange.release.set()
        result = await recheck
        actor._timer.cancel()
        return result

    result = asyncio.run(run())

    start, periodic = _rows(records, ReconciliationRow)
    assert start.trigger is ReconcileTrigger.START
    assert result.reconciliation_id == periodic.id
    assert result.outcome is ReconcileOutcome.MISMATCH


# The exchange snapshot -----------------------------------------------------------------------


def test_every_fetch_publishes_the_exchange_snapshot_unknown_when_it_failed():
    published = []

    async def publish(data):
        published.append(data)

    view = ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=7)
    actor, _, _ = _actor(FakeExchange(view, view, ExchangeUnavailable("down")), publish=publish)

    asyncio.run(actor.check(ReconcileTrigger.START))
    asyncio.run(actor.check(ReconcileTrigger.PERIODIC))

    ok, unknown = published
    assert (ok["status"], ok["fetched_ns"], len(ok["orders"])) == ("ok", 7, 1)
    assert unknown["status"] == "unknown"
    assert "orders" not in unknown
    assert "positions" not in unknown


# Kraken's view, fail closed (AC-4) -----------------------------------------------------------


def _order_report(**kwargs):
    fields = {
        "account_id": AccountId("KRAKEN-UNIFIED"),
        "instrument_id": INSTRUMENT.id,
        "venue_order_id": VenueOrderId("V-1"),
        "order_side": NautilusOrderSide.SELL,
        "order_type": OrderType.STOP_MARKET,
        "time_in_force": TimeInForce.GTC,
        "order_status": OrderStatus.ACCEPTED,
        "quantity": Quantity.from_str("0.0010"),
        "filled_qty": Quantity.from_str("0.0000"),
        "report_id": UUID4(),
        "ts_accepted": 0,
        "ts_last": 0,
        "ts_init": 0,
        "client_order_id": ClientOrderId("O-1"),
        "trigger_price": Price.from_str("60000.0"),
        "trigger_type": TriggerType.MARK_PRICE,
        "reduce_only": True,
    }
    return OrderStatusReport(**(fields | kwargs))


def _position_report(side, quantity):
    return PositionStatusReport(
        account_id=AccountId("KRAKEN-UNIFIED"),
        instrument_id=INSTRUMENT.id,
        position_side=side,
        quantity=Quantity.from_str(quantity),
        report_id=UUID4(),
        ts_last=0,
        ts_init=0,
    )


class FakeReports:
    def __init__(self, orders=(), positions=(), error=None, delay_s=0.0):
        self.orders, self.positions, self.error, self.delay_s = orders, positions, error, delay_s

    async def order_reports(self):
        await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return list(self.orders)

    async def position_reports(self):
        return list(self.positions)

    async def fill_reports(self):
        return []

    async def cancel(self, order):
        raise AssertionError("a fetch never cancels")


def test_kraken_view_turns_reports_into_plain_values_and_drops_flat_positions():
    reports = FakeReports(
        orders=[_order_report()],
        positions=[
            _position_report(NautilusPositionSide.LONG, "0.0010"),
            _position_report(NautilusPositionSide.FLAT, "0.0000"),
        ],
    )

    view = asyncio.run(KrakenExchangeView(reports, 1.0, now_ns=lambda: 42).fetch())

    (order,) = view.orders
    assert (order.client_order_id, order.venue_order_id, order.side) == ("O-1", "V-1", OrderSide.SELL)
    assert (order.order_type, order.quantity, order.trigger_price) == (
        "stop_market",
        Decimal("0.0010"),
        Decimal("60000.0"),
    )
    assert order.reduce_only is True
    (position,) = view.positions
    assert (position.side, position.quantity) == (PositionSide.LONG, Decimal("0.0010"))
    assert view.fetched_ns == 42


def test_kraken_view_keeps_an_order_without_a_client_order_id():
    view = asyncio.run(KrakenExchangeView(FakeReports(orders=[_order_report(client_order_id=None)]), 1.0).fetch())

    assert view.orders[0].client_order_id is None


@pytest.mark.parametrize(
    "reports",
    [
        FakeReports(error=RuntimeError("HTTP 503 Service Unavailable")),
        FakeReports(error=ValueError("expected value at line 1 column 1")),  # malformed JSON
        FakeReports(delay_s=1.0),  # past the timeout
        FakeReports(orders=[object()]),  # a report that does not parse
    ],
    ids=["http_503", "malformed_json", "timeout", "bad_report"],
)
def test_kraken_view_raises_on_any_failure_never_an_empty_view(reports):
    with pytest.raises(ExchangeUnavailable):
        asyncio.run(KrakenExchangeView(reports, 0.05).fetch())


# The start fetch and the watchdog (AC-5) ----------------------------------------------------


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def test_the_start_fetch_retries_with_backoff_and_keeps_the_heartbeat_fresh():
    clock = FakeTime()
    touches = []
    exchange = FakeExchange(ExchangeUnavailable("down"), ExchangeUnavailable("down"), EMPTY)

    view = asyncio.run(
        wait_for_exchange(
            exchange, timeout_s=300, retry_s=5, touch=lambda: touches.append(clock.t), now=clock.now, sleep=clock.sleep
        )
    )

    assert view is EMPTY
    assert clock.sleeps == [5, 10]
    assert touches == [0, 5, 5, 15, 15]  # each attempt, and after each wait


def test_the_start_fetch_never_lets_the_heartbeat_go_older_than_the_healthcheck_allows():
    clock = FakeTime()
    touches = []

    view = asyncio.run(
        wait_for_exchange(
            FakeExchange(ExchangeUnavailable("down")),
            timeout_s=300,
            retry_s=5,
            touch=lambda: touches.append(clock.t),
            now=clock.now,
            sleep=clock.sleep,
        )
    )

    assert view is None
    gaps = [b - a for a, b in itertools.pairwise(touches)]
    assert max(gaps) <= TOUCH_EVERY_S < 30  # the healthcheck's limit
    assert clock.t == 300


def test_the_start_fetch_gives_up_after_the_timeout(caplog):
    clock = FakeTime()

    with caplog.at_level(logging.CRITICAL):
        view = asyncio.run(
            wait_for_exchange(
                FakeExchange(ExchangeUnavailable("down")),
                timeout_s=12,
                retry_s=5,
                touch=lambda: None,
                now=clock.now,
                sleep=clock.sleep,
            )
        )

    assert view is None
    assert clock.t == 12  # the last wait is cut to the timeout
    assert any(getattr(r, "fields", {}).get("code") == "reconcile_start_timeout" for r in caplog.records)


def test_the_watchdog_stops_a_node_whose_trader_never_runs(monkeypatch):
    clock = FakeTime()
    stopped = []
    real_sleep = asyncio.sleep

    async def advance(seconds):
        clock.t += seconds
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", advance)  # watch_start sleeps between polls

    result = asyncio.run(watch_start(lambda: False, lambda: stopped.append(True), 60.0, poll_s=1.0, now=clock.now))

    assert result is False
    assert stopped == [True]
    assert clock.t == 60


def test_the_watchdog_is_quiet_once_the_trader_runs():
    stopped = []

    assert asyncio.run(watch_start(lambda: True, lambda: stopped.append(True), 60.0)) is True
    assert stopped == []
