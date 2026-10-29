"""``ReconcileActor``: the trader's check of its orders and positions against the exchange's (spec 0023).

Registered before any strategy. At start it blocks entries with ``reconciling`` and runs a ``start``
check: fetch the exchange's view through an ``ExchangePort`` (fail closed, AC-4), read the local
view from the Nautilus cache, and ``compare`` the two. A clean check lifts ``reconciling``; a
latched halt stays as it is, since the halt is the gate's, not a block. Checks never overlap: a
trigger that arrives while one runs is queued once.

Every ``start`` and ``reconnect`` check, and every ``periodic`` one that is not clean, writes a
``reconciliations`` row through the recording queue, and each finding one
``reconciliation_mismatches`` row, one critical ``reconcile_mismatch`` risk event keyed
``reconcile:<mismatch id>``, and one CRITICAL log line with that ``event_key`` (AC-13, AC-17). A
check that cannot write Postgres still decides: the queue spools. Without a run (live, until the
drill run lands) rows are not written, only logged.

For now every check only reports (the demo rule of AC-23): no finding is acted on.

``SandboxExchangeView`` is demo's exchange: the simulated exchange's open orders, and the cache's
positions (the sandbox keeps no position book of its own). ``local_view`` reads the cache.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.node.control import Record
from trading_core.reconcile.compare import Finding, compare
from trading_core.reconcile.views import (
    ExchangePort,
    ExchangeUnavailable,
    ExchangeView,
    LocalView,
    OrderView,
    PositionView,
)
from trading_core.recording.enums import (
    Env,
    MismatchAction,
    OrderSide,
    PositionSide,
    ReconcileOutcome,
    ReconcileTrigger,
    RiskEventKind,
    Severity,
)
from trading_core.recording.mappers import snake
from trading_core.recording.rows import ReconciliationMismatchRow, ReconciliationRow, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

EVENT_KEY_PREFIX: Final = "reconcile:"


def order_view(order: Order) -> OrderView:
    """A Nautilus order as plain values."""
    trigger = getattr(order, "trigger_price", None)
    venue_order_id = order.venue_order_id
    return OrderView(
        instrument_id=str(order.instrument_id),
        client_order_id=str(order.client_order_id),
        venue_order_id=None if venue_order_id is None else str(venue_order_id),
        side=OrderSide(order.side.name.lower()),
        order_type=snake(order.order_type.name),
        quantity=order.quantity.as_decimal(),
        trigger_price=None if trigger is None else trigger.as_decimal(),
        reduce_only=bool(order.is_reduce_only),
    )


def position_view(position: Position) -> PositionView:
    """A Nautilus position as plain values."""
    return PositionView(
        instrument_id=str(position.instrument_id),
        side=PositionSide(position.side.name.lower()),
        quantity=position.quantity.as_decimal(),
    )


def local_view(cache: Cache) -> LocalView:
    """The cache's open orders and positions, with each instrument's size increment."""
    orders = tuple(order_view(order) for order in cache.orders_open())
    positions = tuple(position_view(position) for position in cache.positions_open())
    increments: dict[str, Decimal] = {}
    for instrument_id in {o.instrument_id for o in orders} | {p.instrument_id for p in positions}:
        instrument = cache.instrument(InstrumentId.from_str(instrument_id))
        if instrument is not None:
            increments[instrument_id] = instrument.size_increment.as_decimal()
    return LocalView(orders=orders, positions=positions, size_increments=increments)


class SandboxExchangeView:
    """Demo's exchange view: the simulated exchange's open orders and the cache's open positions (AC-23).

    ``client`` returns the sandbox execution client once the node built it, else None.
    """

    def __init__(
        self, client: Callable[[], SandboxExecutionClient | None], cache: Cache, now_ns: Callable[[], int]
    ) -> None:
        self._client = client
        self._cache = cache
        self._now_ns = now_ns

    async def fetch(self) -> ExchangeView:
        """The sandbox's view; ``ExchangeUnavailable`` before the client exists."""
        client = self._client()
        if client is None:
            raise ExchangeUnavailable("sandbox execution client not built")
        orders = tuple(order_view(order) for order in client.exchange.get_open_orders())
        positions = tuple(position_view(position) for position in self._cache.positions_open())
        return ExchangeView(orders=orders, positions=positions, fetched_ns=self._now_ns())


@dataclass(frozen=True)
class CheckResult:
    """How one check ended: its outcome, its report id, and why it failed (None unless it failed)."""

    outcome: ReconcileOutcome
    reconciliation_id: UUID
    error: str | None = None


class ReconcileActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs reconciliation checks and reports them (see the module docstring).

    ``record`` and ``run_id`` are the paper loop's (demo); without them (live) checks are only logged.
    """

    def __init__(
        self,
        *,
        env: Env,
        exchange: ExchangePort,
        blocks: EntryBlocks,
        record: Record | None = None,
        run_id: UUID | None = None,
        local: Callable[[Cache], LocalView] = local_view,
    ) -> None:
        super().__init__(ActorConfig(component_id="ReconcileActor"))
        self._env = env
        self._exchange = exchange
        self._blocks = blocks
        self._record = record
        self._run_id = run_id
        self._local = local
        self._lock = threading.Lock()
        self._last_clean_ns: int | None = None
        self._last_outcome: ReconcileOutcome | None = None
        self._task: asyncio.Task[None] | None = None
        self._queued: ReconcileTrigger | None = None

    # Read by the snapshot -------------------------------------------------------------------

    def snapshot_fields(self) -> dict[str, object]:
        """The check fields of ``trader_state`` (AC-19): the last clean check's time (UTC ns) and the last outcome."""
        with self._lock:
            return {
                "last_clean_check_ns": self._last_clean_ns,
                "last_check_outcome": None if self._last_outcome is None else str(self._last_outcome),
            }

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Block entries with ``reconciling``, then run the ``start`` check on the node's event loop."""
        self._blocks.add(BlockReason.RECONCILING)
        self.request_check(ReconcileTrigger.START)

    def on_stop(self) -> None:
        """Cancel a running check."""
        if self._task is not None:
            self._task.cancel()

    def request_check(self, trigger: ReconcileTrigger) -> None:
        """Run a check now, or once after the running one when one runs (queued once)."""
        if self._task is not None and not self._task.done():
            if self._queued is None:
                self._queued = trigger
            return
        self._task = asyncio.get_running_loop().create_task(self._run(trigger))
        self._task.add_done_callback(_check_ended)

    async def _run(self, trigger: ReconcileTrigger) -> None:
        next_trigger: ReconcileTrigger | None = trigger
        while next_trigger is not None:
            await self.check(next_trigger)
            next_trigger, self._queued = self._queued, None

    # One check ------------------------------------------------------------------------------

    async def check(self, trigger: ReconcileTrigger) -> CheckResult:
        """Fetch, compare, and report one check; never raises for an exchange failure."""
        started_ns = self.clock.timestamp_ns()
        try:
            exchange = await self._exchange.fetch()
        except ExchangeUnavailable as exc:
            row = ReconciliationRow(
                env=self._env,
                run_id=self._run_id or _NO_RUN,
                trigger=trigger,
                started_ns=started_ns,
                finished_ns=self.clock.timestamp_ns(),
                outcome=ReconcileOutcome.FAILED,
                error=str(exc) or type(exc).__name__,
            )
            log.warning(
                "reconciliation check failed, the exchange view is unknown",
                extra={"fields": {"code": "reconcile_failed", "trigger": str(trigger), "error": row.error}},
            )
            self._finish(row, ())
            return CheckResult(ReconcileOutcome.FAILED, row.id, row.error)
        comparison = compare(exchange, self._local(self.cache))
        outcome = ReconcileOutcome.CLEAN if comparison.clean else ReconcileOutcome.MISMATCH
        row = ReconciliationRow(
            env=self._env,
            run_id=self._run_id or _NO_RUN,
            trigger=trigger,
            started_ns=started_ns,
            finished_ns=self.clock.timestamp_ns(),
            outcome=outcome,
            exchange_orders=len(exchange.orders),
            exchange_positions=len(exchange.positions),
            mismatch_count=len(comparison.findings),
        )
        self._finish(row, comparison.findings)
        return CheckResult(outcome, row.id)

    def _finish(self, row: ReconciliationRow, findings: tuple[Finding, ...]) -> None:
        outcome = row.outcome
        with self._lock:
            self._last_outcome = outcome
            if outcome is ReconcileOutcome.CLEAN:
                self._last_clean_ns = row.finished_ns
        if outcome is ReconcileOutcome.CLEAN and self._blocks.clear(BlockReason.RECONCILING):
            log.info(
                "reconciliation clean, entries allowed",
                extra={"fields": {"code": "reconcile_clean", "trigger": str(row.trigger)}},
            )
        reported = row.trigger is not ReconcileTrigger.PERIODIC or outcome is not ReconcileOutcome.CLEAN
        if reported and self._record is not None and self._run_id is not None:
            self._record(WriteItem(WriteCall.RECORD_RECONCILIATION, row))
        log.info(
            "reconciliation check",
            extra={
                "fields": {
                    "code": "reconcile_check",
                    "trigger": str(row.trigger),
                    "outcome": None if outcome is None else str(outcome),
                    "reconciliation_id": str(row.id),
                    "exchange_orders": row.exchange_orders,
                    "exchange_positions": row.exchange_positions,
                    "mismatch_count": row.mismatch_count,
                }
            },
        )
        self._report_findings(row, findings)

    def _report_findings(self, row: ReconciliationRow, findings: Iterable[Finding]) -> None:
        for finding in findings:
            mismatch = ReconciliationMismatchRow(
                env=self._env,
                reconciliation_id=row.id,
                kind=finding.kind,
                instrument_id=finding.instrument_id,
                client_order_id=finding.client_order_id,
                venue_order_id=finding.venue_order_id,
                exchange_side=finding.exchange_side,
                exchange_qty=finding.exchange_qty,
                local_side=finding.local_side,
                local_qty=finding.local_qty,
                action=MismatchAction.NONE,
                details={"trigger": str(row.trigger)},
            )
            key = f"{EVENT_KEY_PREFIX}{mismatch.id}"
            message = f"reconciliation found {finding.kind} on {finding.instrument_id}"
            log.critical(
                message,
                extra={
                    "fields": {
                        "code": "reconcile_mismatch",
                        "event_key": key,
                        "kind": str(finding.kind),
                        "instrument_id": finding.instrument_id,
                        "client_order_id": finding.client_order_id,
                        "venue_order_id": finding.venue_order_id,
                        "action": str(mismatch.action),
                    }
                },
            )
            if self._record is None or self._run_id is None:
                continue
            self._record(WriteItem(WriteCall.RECORD_RECONCILIATION_MISMATCH, mismatch))
            self._record(
                WriteItem(
                    WriteCall.RECORD_RISK_EVENT,
                    RiskEventRow(
                        env=self._env,
                        event_key=key,
                        kind=RiskEventKind.RECONCILE_MISMATCH,
                        severity=Severity.CRITICAL,
                        message=message,
                        details={"reconciliation_id": str(row.id), "mismatch_id": str(mismatch.id)},
                        run_id=self._run_id,
                        occurred_ns=row.finished_ns or row.started_ns,
                    ),
                )
            )


# A row needs a run id even when it is not written (live has no run yet); it is never recorded.
_NO_RUN: Final = UUID(int=0)


def _check_ended(task: asyncio.Task[None]) -> None:
    if task.cancelled() or task.exception() is None:
        return
    log.critical(
        "reconciliation check crashed, entries stay blocked",
        exc_info=task.exception(),
        extra={"fields": {"code": "reconcile_crashed"}},
    )
