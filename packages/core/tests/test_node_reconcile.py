"""covers: spec 0023 AC-1 (the start check and the ``reconciling`` block), AC-4 (a failed fetch is ``failed``),
AC-13 (each finding's row, risk event, and CRITICAL log), AC-17 (which checks write a row), AC-23 (demo only reports).

The actor is registered with a BacktestEngine for its clock and cache, then driven directly with
a fake exchange port inside ``asyncio.run``. Needs the ``node`` extra.
"""

import asyncio
import logging
import uuid
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_placeholder import INSTRUMENT

from trading_core.node.reconcile import EVENT_KEY_PREFIX, ReconcileActor
from trading_core.reconcile.views import ExchangeUnavailable, ExchangeView, LocalView, OrderView
from trading_core.recording.enums import (
    Env,
    MismatchAction,
    MismatchKind,
    OrderSide,
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


def _actor(exchange, *, record=True, local=None):
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
        **({} if local is None else {"local": lambda _cache: local}),
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
    assert fields == {"last_clean_check_ns": row.finished_ns, "last_check_outcome": "clean"}


def test_before_any_check_the_snapshot_fields_are_null():
    actor, _, _ = _actor(FakeExchange(EMPTY))

    assert actor.snapshot_fields() == {"last_clean_check_ns": None, "last_check_outcome": None}


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
    assert actor.snapshot_fields() == {"last_clean_check_ns": None, "last_check_outcome": "mismatch"}


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
    assert blocks.active() == (BlockReason.RECONCILING,)
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
    actor, _, records = _actor(FakeExchange(ExchangeView(orders=(FOREIGN,), positions=(), fetched_ns=1), EMPTY))

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
