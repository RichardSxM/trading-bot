"""``ReconcileActor``: the trader's check of its orders and positions against the exchange's (spec 0023).

Registered before any strategy. At start it blocks entries with ``reconciling`` and runs a ``start``
check: fetch the exchange's view through an ``ExchangePort`` (fail closed, AC-4), read the local
view from the Nautilus cache, and ``compare`` the two. A clean check lifts ``reconciling`` and
``exchange_unknown``; a latched halt stays as it is, since the halt is the gate's, not a block.
Checks never overlap: a trigger that arrives while one runs is queued once.

After each check the next one is scheduled: a ``periodic`` check ``interval_s`` later (AC-2), or,
when the fetch failed, a retry of the same trigger after a backoff that starts at ``retry_s`` and
doubles up to 60 s, with entries blocked ``exchange_unknown`` until a clean check (AC-4). When the
``stale_feed`` block goes on, ``reconciling`` goes on with it; when it clears (the network is back),
a ``reconnect`` check runs at once (AC-3). ``recheck`` runs a ``periodic`` check at once and answers
with the result of the next check to start (AC-20).

Every ``start`` and ``reconnect`` check, and every ``periodic`` one that is not clean, writes a
``reconciliations`` row through the recording queue, and each finding one
``reconciliation_mismatches`` row, one critical ``reconcile_mismatch`` risk event keyed
``reconcile:<mismatch id>``, and one CRITICAL log line with that ``event_key`` (AC-13, AC-17). A
check that cannot write Postgres still decides: the queue spools. Without a run (live, until the
drill run lands) rows are not written, only logged. After every fetch the actor publishes the
``{env}:snapshot:exchange`` data (``unknown`` when the fetch failed).

A check compares only a quiet fetch: nothing of ours in flight and no order event while it fetched,
else it fetches again, and after ``QUIET_TRIES`` it ends ``failed`` with ``not_quiet``. Findings
count only when a second quiet fetch finds them too (AC-27). ``plan`` then decides each finding's
action. In demo (no ``Actions``) nothing is acted on (AC-23); while the kill switch flattens or the
trader is halted only unknown entry orders are cancelled (AC-28). Otherwise the steps run in their
fixed order (cancel, mark, align the cache, fix stops) and a halting finding latches a
``reconcile_mismatch`` halt through the kill switch last (AC-7 to AC-12). A mismatch that does not
halt keeps entries blocked with ``reconciling`` until the next clean check (live only).

``SandboxExchangeView`` is demo's exchange: the simulated exchange's open orders, and the cache's
positions (the sandbox keeps no position book of its own). ``KrakenExchangeView`` is live's: the
Kraken Futures HTTP client's open order and position reports, called directly, because the
adapter's own report methods turn errors into empty lists. ``local_view`` reads the cache.
``wait_for_exchange`` is the live start fetch before the node is built (AC-5), and
``watch_start`` the watchdog that stops a node whose trader never started.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport
from nautilus_trader.model.enums import OrderStatus, OrderType
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, VenueOrderId
from nautilus_trader.model.orders.base import Order
from nautilus_trader.model.position import Position

from trading_core.control.consumer import Outcome
from trading_core.control.halt import HaltReason
from trading_core.node.control import Record
from trading_core.reconcile.compare import Comparison, Finding, compare
from trading_core.reconcile.plan import (
    CancelOrder,
    CloseAtMarket,
    MarkCanceled,
    Mode,
    PlaceStop,
    Plan,
    Step,
    SyncPosition,
    plan,
)
from trading_core.reconcile.views import (
    CancelPort,
    ExchangePort,
    ExchangeUnavailable,
    ExchangeView,
    FillOrder,
    HeldPosition,
    LocalView,
    OrderView,
    PositionView,
    exchange_snapshot_data,
)
from trading_core.recording.enums import (
    ActionResult,
    ControlOutcome,
    Env,
    MismatchAction,
    OrderSide,
    PositionSide,
    ReconcileOutcome,
    ReconcileTrigger,
    RiskEventKind,
    Severity,
)
from trading_core.recording.mappers import PARENT_TAG, snake
from trading_core.recording.rows import (
    JsonObject,
    JsonValue,
    ReconciliationMismatchRow,
    ReconciliationRow,
    RiskEventRow,
)
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

EVENT_KEY_PREFIX: Final = "reconcile:"
# The longest wait between retries of a failed fetch (AC-4).
MAX_RETRY_S: Final = 60.0
# How often the start fetch touches the heartbeat file while it waits; the healthcheck allows 30 s.
TOUCH_EVERY_S: Final = 10.0
# The account id Kraken's reports carry (the adapter's own, ``KRAKEN-UNIFIED``).
KRAKEN_ACCOUNT_ID: Final = "KRAKEN-UNIFIED"
# Nautilus 1.231.0's marks on an order it made during reconciliation (AC-6): the strategy id
# ``EXTERNAL``, tagged ``RECONCILIATION`` (a size fix) or ``VENUE`` (an order found on the venue).
GENERATED_STRATEGY: Final = "EXTERNAL"
GENERATED_TAGS: Final = frozenset({"RECONCILIATION", "VENUE"})
# A local only order that never got a venue order id is reported canceled under this made up one.
LOCAL_ONLY_VENUE_PREFIX: Final = "RECON-"
# AC-27: tries for a quiet fetch, and the wait between them.
QUIET_TRIES: Final = 3
QUIET_WAIT_S: Final = 0.5
NOT_QUIET: Final = "not_quiet"
# How often a stop fix looks for a fresh quote while it waits (AC-9).
QUOTE_POLL_S: Final = 0.25
STOP_REFUSED: Final = "stop_refused"

Publish = Callable[[Mapping[str, object]], Awaitable[None]]


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
        strategy_id=str(order.strategy_id),
        ts_init=order.ts_init,
    )


def position_view(position: Position) -> PositionView:
    """A Nautilus position as plain values."""
    return PositionView(
        instrument_id=str(position.instrument_id),
        side=PositionSide(position.side.name.lower()),
        quantity=position.quantity.as_decimal(),
    )


def is_generated(order: Order) -> bool:
    """True for an order Nautilus made during reconciliation: never ours (AC-6)."""
    tags = order.tags or []
    return str(order.strategy_id) == GENERATED_STRATEGY or any(tag in GENERATED_TAGS for tag in tags)


def held_position(position: Position, cache: Cache) -> HeldPosition:
    """A cached position with the orders that filled into it; an order missing from the cache counts as not ours."""
    fill_orders: list[FillOrder] = []
    for client_order_id in position.client_order_ids:
        order = cache.order(client_order_id)
        if order is None:
            fill_orders.append(FillOrder(client_order_id=str(client_order_id), strategy_id="", reduce_only=False))
            continue
        fill_orders.append(
            FillOrder(
                client_order_id=str(client_order_id),
                strategy_id=str(order.strategy_id),
                reduce_only=bool(order.is_reduce_only),
                generated=is_generated(order),
            )
        )
    return HeldPosition(
        instrument_id=str(position.instrument_id),
        side=PositionSide(position.side.name.lower()),
        quantity=position.quantity.as_decimal(),
        position_id=str(position.id),
        strategy_id=str(position.strategy_id),
        opening_order_id=str(position.opening_order_id),
        ts_opened=position.ts_opened,
        fill_orders=tuple(fill_orders),
    )


def cached_stop_trigger(cache: Cache, position: Position) -> Decimal | None:
    """The trigger of the newest reduce only stop market order in the cache tagged with the position's opening
    order (any status: a stop cancelled while the trader was down still tells its price)."""
    parent = f"{PARENT_TAG}{position.opening_order_id}"
    stops = [
        order
        for order in cache.orders(instrument_id=position.instrument_id)
        if order.order_type == OrderType.STOP_MARKET and order.is_reduce_only and parent in (order.tags or [])
    ]
    if not stops:
        return None
    newest = max(stops, key=lambda order: order.ts_init)
    trigger = getattr(newest, "trigger_price", None)
    return None if trigger is None else trigger.as_decimal()


# The Postgres fallback for a stop's trigger price: the opening order's client order id → the newest
# recorded ``orders.trigger_price`` of a reduce only stop market order tagged with it.
StopTriggerLookup = Callable[[str], Decimal | None]


def local_view(
    cache: Cache, owned: frozenset[str] = frozenset(), recorded: StopTriggerLookup | None = None
) -> LocalView:
    """The cache's open orders of ours and its open positions, with each instrument's size increment and the
    recorded trigger price of each position's stop (spec 0023 *Value sourcing*)."""
    orders = tuple(
        order_view(order)
        for order in cache.orders_open()
        if str(order.strategy_id) in owned and not is_generated(order)
    )
    open_positions = cache.positions_open()
    positions = tuple(held_position(position, cache) for position in open_positions)
    triggers: dict[str, Decimal] = {}
    for position in open_positions:
        trigger = cached_stop_trigger(cache, position)
        if trigger is None and recorded is not None:
            trigger = recorded(str(position.opening_order_id))
        if trigger is not None:
            triggers[str(position.instrument_id)] = trigger
    increments: dict[str, Decimal] = {}
    for instrument_id in {o.instrument_id for o in orders} | {p.instrument_id for p in positions}:
        instrument = cache.instrument(InstrumentId.from_str(instrument_id))
        if instrument is not None:
            increments[instrument_id] = instrument.size_increment.as_decimal()
    return LocalView(
        orders=orders, positions=positions, size_increments=increments, owned_strategies=owned, stop_triggers=triggers
    )


def fresh_mids(cache: Cache, instrument_ids: Iterable[str], now_ns: int, max_age_ns: int) -> dict[str, Decimal]:
    """The last quote's mid per instrument, only for quotes no older than ``max_age_ns`` (AC-9)."""
    mids: dict[str, Decimal] = {}
    for instrument_id in instrument_ids:
        quote = cache.quote_tick(InstrumentId.from_str(instrument_id))
        if quote is None or now_ns - quote.ts_event > max_age_ns:
            continue
        mids[instrument_id] = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
    return mids


def busy(cache: Cache) -> bool:
    """True while an order is in flight, pending cancel, or pending update: the views may disagree for a moment."""
    if cache.orders_inflight():
        return True
    return any(order.is_pending_cancel or order.is_pending_update for order in cache.orders_open())


def report_order_view(report: OrderStatusReport) -> OrderView:
    """A Kraken order status report as plain values."""
    client_order_id = report.client_order_id
    trigger = report.trigger_price
    return OrderView(
        instrument_id=str(report.instrument_id),
        client_order_id=None if client_order_id is None else str(client_order_id),
        venue_order_id=str(report.venue_order_id),
        side=OrderSide(report.order_side.name.lower()),
        order_type=snake(report.order_type.name),
        quantity=report.quantity.as_decimal(),
        trigger_price=None if trigger is None else trigger.as_decimal(),
        reduce_only=bool(report.reduce_only),
    )


def report_position_view(report: PositionStatusReport) -> PositionView | None:
    """A Kraken position status report as plain values; None for a flat one."""
    quantity = report.quantity.as_decimal()
    if report.position_side == NautilusPositionSide.FLAT or quantity == 0:
        return None
    return PositionView(
        instrument_id=str(report.instrument_id),
        side=PositionSide(report.position_side.name.lower()),
        quantity=quantity,
    )


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


class FuturesReports(Protocol):
    """The two Kraken Futures report calls a ``KrakenExchangeView`` makes."""

    async def order_reports(self) -> Sequence[OrderStatusReport]:
        """Kraken's open orders."""
        ...

    async def position_reports(self) -> Sequence[PositionStatusReport]:
        """Kraken's positions."""
        ...

    async def cancel(self, order: OrderView) -> None:
        """Cancel one order on Kraken by its venue order id, else its client order id."""
        ...


class PyO3FuturesReports:
    """``FuturesReports`` through the Kraken Futures HTTP client; loads the instruments once, on first use."""

    def __init__(self, http: nautilus_pyo3.KrakenFuturesHttpClient) -> None:
        self._http = http
        self._account_id = nautilus_pyo3.AccountId(KRAKEN_ACCOUNT_ID)
        self._instruments_loaded = False

    async def _load_instruments(self) -> None:
        if self._instruments_loaded:
            return
        for instrument in await self._http.request_instruments():
            self._http.cache_instrument(instrument)
        self._instruments_loaded = True

    async def order_reports(self) -> Sequence[OrderStatusReport]:
        """Kraken's open orders, as Nautilus reports."""
        await self._load_instruments()
        reports = await self._http.request_order_status_reports(account_id=self._account_id, open_only=True)
        return [OrderStatusReport.from_pyo3(report) for report in reports]

    async def position_reports(self) -> Sequence[PositionStatusReport]:
        """Kraken's positions, as Nautilus reports."""
        await self._load_instruments()
        reports = await self._http.request_position_status_reports(account_id=self._account_id)
        return [PositionStatusReport.from_pyo3(report) for report in reports]

    async def cancel(self, order: OrderView) -> None:
        """Cancel one order on Kraken by its venue order id, else its client order id."""
        await self._load_instruments()
        venue_order_id = None if order.venue_order_id is None else nautilus_pyo3.VenueOrderId(order.venue_order_id)
        client_order_id = (
            None
            if venue_order_id is not None or order.client_order_id is None
            else nautilus_pyo3.ClientOrderId(order.client_order_id)
        )
        await self._http.cancel_order(
            self._account_id,
            nautilus_pyo3.InstrumentId.from_str(order.instrument_id),
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
        )


def kraken_futures_reports(
    api_key: str, api_secret: str, timeout_s: int, base_url: str | None = None
) -> PyO3FuturesReports:
    """A Kraken Futures HTTP client of its own for the checks, keys set explicitly from the secret files.

    It does not retry by itself: a failure reaches the check at once, which retries with its own
    backoff. ``base_url`` exists only for the canary leak test, which points it at a closed port.
    """
    return PyO3FuturesReports(
        nautilus_pyo3.KrakenFuturesHttpClient(
            api_key=api_key, api_secret=api_secret, base_url=base_url, timeout_secs=timeout_s, max_retries=0
        )
    )


class KrakenExchangeView:
    """Live's exchange view: Kraken's open orders and open positions, fail closed (AC-4).

    Each request gets ``timeout_s``. Any error, timeout, HTTP failure, or report that does not
    parse raises ``ExchangeUnavailable``; it never reads as "no orders, no position".
    """

    def __init__(self, reports: FuturesReports, timeout_s: float, now_ns: Callable[[], int] = time.time_ns) -> None:
        self._reports = reports
        self._timeout_s = timeout_s
        self._now_ns = now_ns

    async def fetch(self) -> ExchangeView:
        """Kraken's view, or ``ExchangeUnavailable``."""
        try:
            async with asyncio.timeout(self._timeout_s):
                orders = tuple(report_order_view(r) for r in await self._reports.order_reports())
            async with asyncio.timeout(self._timeout_s):
                reported = [report_position_view(r) for r in await self._reports.position_reports()]
        except Exception as exc:  # fail closed: whatever went wrong, Kraken's view is unknown
            raise ExchangeUnavailable(_describe(exc)) from exc
        positions = tuple(p for p in reported if p is not None)
        return ExchangeView(orders=orders, positions=positions, fetched_ns=self._now_ns())

    async def cancel(self, order: OrderView) -> None:
        """Cancel ``order`` on Kraken directly (also one the cache does not hold); ``ExchangeUnavailable`` unless
        Kraken confirmed it."""
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._reports.cancel(order)
        except Exception as exc:  # fail closed: the order may still be open
            raise ExchangeUnavailable(_describe(exc)) from exc


class LiveExchange(ExchangePort, CancelPort, Protocol):
    """Live's exchange: fetches the view and cancels orders directly (``KrakenExchangeView``)."""


class ReportSink(Protocol):
    """Feeds a report to Nautilus's execution engine, the path its own reconciliation uses (AC-8, AC-11)."""

    def mark_canceled(self, order: OrderView) -> None:
        """Mark a cached order canceled; raises ``ReconcileActionError`` when Nautilus refused it."""
        ...

    def sync_position(self, instrument_id: str, exchange: PositionView | None) -> None:
        """Make the cache's net position on the instrument match the exchange's (None: flat)."""
        ...


class ReconcileActionError(Exception):
    """A reconciliation action could not be applied."""


class ReconcilingEngine(Protocol):
    """The one ``LiveExecutionEngine`` call the sink makes."""

    def reconcile_execution_report(self, report: OrderStatusReport | PositionStatusReport) -> bool: ...


class ExecEngineReports:
    """``ReportSink`` through the live execution engine's ``reconcile_execution_report``."""

    def __init__(self, engine: ReconcilingEngine, cache: Cache, now_ns: Callable[[], int] = time.time_ns) -> None:
        self._engine = engine
        self._cache = cache
        self._now_ns = now_ns
        self._account_id = AccountId(KRAKEN_ACCOUNT_ID)

    def mark_canceled(self, order: OrderView) -> None:
        """Feed a ``CANCELED`` order status report for the cached order."""
        if order.client_order_id is None:
            raise ReconcileActionError("a local order without a client order id")
        cached = self._cache.order(ClientOrderId(order.client_order_id))
        if cached is None:
            raise ReconcileActionError(f"order {order.client_order_id} not in the cache")
        now = self._now_ns()
        venue_order_id = cached.venue_order_id or VenueOrderId(f"{LOCAL_ONLY_VENUE_PREFIX}{order.client_order_id}")
        report = OrderStatusReport(
            account_id=cached.account_id or self._account_id,
            instrument_id=cached.instrument_id,
            venue_order_id=venue_order_id,
            order_side=cached.side,
            order_type=cached.order_type,
            time_in_force=cached.time_in_force,
            order_status=OrderStatus.CANCELED,
            quantity=cached.quantity,
            filled_qty=cached.filled_qty,
            report_id=UUID4(),
            ts_accepted=cached.ts_init,
            ts_last=now,
            ts_init=now,
            client_order_id=cached.client_order_id,
            reduce_only=bool(cached.is_reduce_only),
            cancel_reason="reconcile_local_only",
        )
        if not self._engine.reconcile_execution_report(report):
            raise ReconcileActionError(f"Nautilus did not reconcile order {order.client_order_id}")

    def sync_position(self, instrument_id: str, exchange: PositionView | None) -> None:
        """Feed a position status report with the exchange's side and size."""
        instrument = self._cache.instrument(InstrumentId.from_str(instrument_id))
        if instrument is None:
            raise ReconcileActionError(f"instrument {instrument_id} not in the cache")
        now = self._now_ns()
        side = NautilusPositionSide.FLAT if exchange is None else NautilusPositionSide[exchange.side.upper()]
        report = PositionStatusReport(
            account_id=self._account_id,
            instrument_id=instrument.id,
            position_side=side,
            quantity=instrument.make_qty(Decimal(0) if exchange is None else exchange.quantity),
            report_id=UUID4(),
            ts_last=now,
            ts_init=now,
        )
        if not self._engine.reconcile_execution_report(report):
            raise ReconcileActionError(f"Nautilus did not reconcile the position on {instrument_id}")


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def wait_for_exchange(
    exchange: ExchangePort,
    *,
    timeout_s: float,
    retry_s: float,
    touch: Callable[[], None],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ExchangeView | None:
    """The live start fetch before the node is built (AC-5): retry with backoff until ``timeout_s``.

    ``touch`` (the heartbeat file) runs on each attempt and every ``TOUCH_EVERY_S`` while it waits,
    so the healthcheck never cuts the retry short. None when Kraken never answered in time.
    """
    started = now()
    delay = retry_s
    attempt = 0
    while True:
        attempt += 1
        touch()
        try:
            view = await exchange.fetch()
        except ExchangeUnavailable as exc:
            waited = now() - started
            if waited >= timeout_s:
                log.critical(
                    "kraken unreachable at start, exiting",
                    extra={"fields": {"code": "reconcile_start_timeout", "attempts": attempt, "error": str(exc)}},
                )
                return None
            wait = min(delay, timeout_s - waited)
            log.warning(
                "kraken unreachable at start, retrying",
                extra={
                    "fields": {"code": "reconcile_start_retry", "attempt": attempt, "error": str(exc), "wait_s": wait}
                },
            )
            while wait > 0:
                step = min(wait, TOUCH_EVERY_S)
                await sleep(step)
                touch()
                wait -= step
            delay = min(delay * 2, MAX_RETRY_S)
            continue
        log.info(
            "kraken reachable at start",
            extra={"fields": {"code": "reconcile_start_fetched", "attempts": attempt}},
        )
        return view


async def watch_start(
    is_running: Callable[[], bool],
    on_failed: Callable[[], None],
    timeout_s: float,
    poll_s: float = 1.0,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """The startup watchdog (AC-5): True once the trader runs; after ``timeout_s`` without it, log and call
    ``on_failed`` (which stops the node), so a node whose Nautilus reconciliation failed never trades."""
    started = now()
    while not is_running():
        if now() - started >= timeout_s:
            log.critical(
                "trader not running after the start timeout, Nautilus reconciliation likely failed, stopping",
                extra={"fields": {"code": "reconcile_start_watchdog", "timeout_s": timeout_s}},
            )
            on_failed()
            return False
        await asyncio.sleep(poll_s)
    return True


@dataclass(frozen=True)
class CheckResult:
    """How one check ended: its outcome, its report id, and why it failed (None unless it failed)."""

    outcome: ReconcileOutcome
    reconciliation_id: UUID
    error: str | None = None


class NotQuiet(Exception):
    """Orders of ours stayed in flight, or fills kept arriving, through every try of a fetch (AC-27)."""


class Executor(Protocol):
    """The order steps the ``ReconcileExecutor`` strategy runs."""

    async def place_stop(self, step: PlaceStop) -> bool:
        """Place the target stop; True once the exchange accepted it."""
        ...

    def close(self, position_id: str) -> None:
        """Close one position at market, reduce only, retrying while it stays open."""
        ...


class Halter(Protocol):
    """What a check needs from the kill switch (``KillSwitch``)."""

    @property
    def flattening(self) -> bool: ...

    @property
    def halted(self) -> bool: ...

    def halt_automatic(self, reason: HaltReason, *, flatten: bool = True) -> UUID: ...


@dataclass(frozen=True)
class Actions:
    """Everything a check that acts needs (live). Without it (demo) a check only reports (AC-23)."""

    executor: Executor
    cancel: CancelPort
    reports: ReportSink
    kill_switch: Halter


class ReconcileActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs reconciliation checks and reports them (see the module docstring).

    ``record`` and ``run_id`` are the paper loop's (demo); without them (live) checks are only logged.
    ``publish`` writes the exchange snapshot; it handles its own errors. ``owned`` returns the ids
    of the strategies whose orders are ours, ``recorded`` is the Postgres fallback for a stop's
    trigger price, and ``actions`` (live) lets a check act; ``local`` replaces the cache read in tests.
    """

    def __init__(
        self,
        *,
        env: Env,
        exchange: ExchangePort,
        blocks: EntryBlocks,
        record: Record | None = None,
        run_id: UUID | None = None,
        local: Callable[[Cache], LocalView] | None = None,
        owned: Callable[[], frozenset[str]] = frozenset,
        recorded: StopTriggerLookup | None = None,
        actions: Actions | None = None,
        interval_s: float = 60.0,
        retry_s: float = 5.0,
        stale_quote_s: float = 10.0,
        quiet_wait_s: float = QUIET_WAIT_S,
        publish: Publish | None = None,
    ) -> None:
        super().__init__(ActorConfig(component_id="ReconcileActor"))
        self._env = env
        self._exchange = exchange
        self._blocks = blocks
        self._record = record
        self._run_id = run_id
        self._owned = owned
        self._recorded = recorded
        self._local = local or (lambda cache: local_view(cache, self._owned(), self._recorded))
        self._actions = actions
        self._stale_quote_s = stale_quote_s
        self._quiet_wait_s = quiet_wait_s
        self._order_events = 0
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._next_retry_s = retry_s
        self._publish = publish
        self._lock = threading.Lock()
        self._last_clean_ns: int | None = None
        self._last_outcome: ReconcileOutcome | None = None
        self._task: asyncio.Task[None] | None = None
        self._queued: ReconcileTrigger | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._waiters: list[asyncio.Future[CheckResult]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = False
        blocks.subscribe(self._on_block)

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
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        self._blocks.add(BlockReason.RECONCILING)
        # Every order event (an acknowledgement, a fill) tells a check its fetch may be stale (AC-27).
        self.msgbus.subscribe(topic="events.order.*", handler=self.note_order_event)
        self.request_check(ReconcileTrigger.START)

    def act_with(self, actions: Actions) -> None:
        """Let checks act (live), once the executor and the kill switch exist."""
        self._actions = actions

    def note_order_event(self, event: object) -> None:
        """Count an order event; a check whose fetch overlapped one fetches again."""
        self._order_events += 1

    def on_stop(self) -> None:
        """Cancel the next check and a running one."""
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is not None:
            self._task.cancel()
        for waiter in self._waiters:
            waiter.cancel()
        self._waiters.clear()

    def _on_block(self, reason: BlockReason, active: bool) -> None:
        # Any thread: the strategy's stale check (AC-3).
        if reason is not BlockReason.STALE_FEED:
            return
        if active:
            self._blocks.add(BlockReason.RECONCILING)
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.request_check, ReconcileTrigger.RECONNECT)

    def request_check(self, trigger: ReconcileTrigger) -> None:
        """Run a check now, or once after the running one when one runs (queued once)."""
        if self._stopped:
            return
        if self._task is not None and not self._task.done():
            if self._queued is None:
                self._queued = trigger
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._task = asyncio.get_running_loop().create_task(self._run(trigger))
        self._task.add_done_callback(_check_ended)

    async def recheck(self) -> CheckResult:
        """Run a ``periodic`` check now and return its result; while one runs, the next check's (AC-20)."""
        if self._stopped:
            return CheckResult(ReconcileOutcome.FAILED, _NO_RUN, "stopped")
        future: asyncio.Future[CheckResult] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        self.request_check(ReconcileTrigger.PERIODIC)
        return await future

    async def recheck_outcome(self) -> Outcome:
        """``recheck`` as a control answer: ``executed`` with the outcome and report id, or ``failed`` with
        ``exchange_unknown`` when Kraken could not be read."""
        result = await self.recheck()
        details = {"outcome": str(result.outcome), "reconciliation_id": str(result.reconciliation_id)}
        if result.outcome is ReconcileOutcome.FAILED:
            return Outcome(ControlOutcome.FAILED, "exchange_unknown", details | {"error": result.error or ""})
        return Outcome(ControlOutcome.EXECUTED, None, details)

    async def _run(self, trigger: ReconcileTrigger) -> None:
        next_trigger: ReconcileTrigger | None = trigger
        last = trigger
        result: CheckResult | None = None
        while next_trigger is not None:
            last = next_trigger
            waiters, self._waiters = self._waiters, []
            try:
                result = await self.check(next_trigger)
            except Exception as exc:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_exception(exc)
                raise
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(result)
            next_trigger, self._queued = self._queued, None
        if result is not None:
            self._schedule_next(result, last)

    def _schedule_next(self, result: CheckResult, trigger: ReconcileTrigger) -> None:
        if self._stopped:
            return
        if result.outcome is ReconcileOutcome.FAILED:
            delay, next_trigger = self._next_retry_s, trigger
            self._next_retry_s = min(self._next_retry_s * 2, MAX_RETRY_S)
        else:
            delay, next_trigger = self._interval_s, ReconcileTrigger.PERIODIC
            self._next_retry_s = self._retry_s
        self._timer = asyncio.get_running_loop().call_later(delay, self.request_check, next_trigger)

    # One check ------------------------------------------------------------------------------

    async def check(self, trigger: ReconcileTrigger) -> CheckResult:
        """Fetch, compare, act on, and report one check; never raises for an exchange failure.

        The compare runs only on a quiet fetch (AC-27), and findings count only when a second quiet
        fetch finds them too. Then ``plan`` decides each finding's action for this check's ``Mode``,
        and the steps run in their fixed order; a halting finding latches a halt last.
        """
        started_ns = self.clock.timestamp_ns()
        try:
            exchange, local = await self._quiet_fetch()
            comparison = compare(exchange, local)
            if not comparison.clean:
                exchange, local = await self._quiet_fetch()
                comparison = _confirmed(comparison, compare(exchange, local))
        except NotQuiet:
            return self._not_quiet(trigger, started_ns)
        except ExchangeUnavailable as exc:
            await self._publish_view(None)
            row = ReconciliationRow(
                env=self._env,
                run_id=self._run_id or _NO_RUN,
                trigger=trigger,
                started_ns=started_ns,
                finished_ns=self.clock.timestamp_ns(),
                outcome=ReconcileOutcome.FAILED,
                error=str(exc) or type(exc).__name__,
            )
            if self._blocks.add(BlockReason.EXCHANGE_UNKNOWN):
                log.warning(
                    "kraken's orders and positions are unknown, entries blocked",
                    extra={"fields": {"code": "exchange_unknown", "trigger": str(trigger), "error": row.error}},
                )
            log.warning(
                "reconciliation check failed, the exchange view is unknown",
                extra={
                    "fields": {
                        "code": "reconcile_failed",
                        "trigger": str(trigger),
                        "error": row.error,
                        "retry_s": self._next_retry_s,
                    }
                },
            )
            self._finish(row, ())
            return CheckResult(ReconcileOutcome.FAILED, row.id, row.error)
        await self._publish_view(exchange)
        mode = self._mode()
        the_plan = plan(comparison, local, mode, self._mids(comparison))
        if the_plan.awaiting_quote:
            await self._await_quotes(the_plan.awaiting_quote)
            the_plan = plan(comparison, local, mode, self._mids(comparison), quote_waited=True)
        outcomes = await self._act(the_plan, mode)
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
        self._finish(row, outcomes)
        if outcome is ReconcileOutcome.MISMATCH and mode is not Mode.DEMO:
            # Entries resume on the next clean check (a halt, if any, holds them past it).
            self._blocks.add(BlockReason.RECONCILING)
        halt = mode is Mode.ACT and any(o.halts for o in outcomes)
        if halt and self._actions is not None:
            halt_id = self._actions.kill_switch.halt_automatic(
                HaltReason.RECONCILE_MISMATCH, flatten=any(o.flattens for o in outcomes)
            )
            log.critical(
                "reconciliation halted the trader",
                extra={"fields": {"code": "reconcile_halt", "reconciliation_id": str(row.id), "halt_id": str(halt_id)}},
            )
        return CheckResult(outcome, row.id)

    async def _quiet_fetch(self) -> tuple[ExchangeView, LocalView]:
        """A fetch and a cache read with nothing of ours in flight and no order event in between (AC-27)."""
        for attempt in range(QUIET_TRIES):
            if attempt:
                await asyncio.sleep(self._quiet_wait_s)
            if busy(self.cache):
                continue
            events = self._order_events
            exchange = await self._exchange.fetch()
            local = self._local(self.cache)
            if events == self._order_events and not busy(self.cache):
                return exchange, local
        raise NotQuiet

    def _not_quiet(self, trigger: ReconcileTrigger, started_ns: int) -> CheckResult:
        row = ReconciliationRow(
            env=self._env,
            run_id=self._run_id or _NO_RUN,
            trigger=trigger,
            started_ns=started_ns,
            finished_ns=self.clock.timestamp_ns(),
            outcome=ReconcileOutcome.FAILED,
            error=NOT_QUIET,
        )
        log.warning(
            "reconciliation check failed, orders kept moving",
            extra={"fields": {"code": "reconcile_not_quiet", "trigger": str(trigger), "tries": QUIET_TRIES}},
        )
        self._finish(row, ())
        return CheckResult(ReconcileOutcome.FAILED, row.id, NOT_QUIET)

    def _mode(self) -> Mode:
        actions = self._actions
        if actions is None:
            return Mode.DEMO
        if actions.kill_switch.flattening or actions.kill_switch.halted:
            return Mode.REPORT
        return Mode.ACT

    def _mids(self, comparison: Comparison) -> dict[str, Decimal]:
        instruments = {f.instrument_id for f in comparison.findings}
        max_age_ns = int(self._stale_quote_s * 1_000_000_000)
        return fresh_mids(self.cache, instruments, self.clock.timestamp_ns(), max_age_ns)

    async def _await_quotes(self, instruments: frozenset[str]) -> None:
        """Wait up to ``stale_quote_s`` for a fresh quote on every instrument (AC-9)."""
        deadline = time.monotonic() + self._stale_quote_s
        max_age_ns = int(self._stale_quote_s * 1_000_000_000)
        while time.monotonic() < deadline:
            if len(fresh_mids(self.cache, instruments, self.clock.timestamp_ns(), max_age_ns)) == len(instruments):
                return
            await asyncio.sleep(QUOTE_POLL_S)

    async def _act(self, the_plan: Plan, mode: Mode) -> list[FindingOutcome]:
        """Run every step in phase order; each finding's result is ``ok`` unless a step failed or is under way."""
        results: dict[int, ActionResult] = {}
        extra: dict[int, dict[str, str]] = {}
        refused: set[int] = set()
        index = {id(p): i for i, p in enumerate(the_plan.planned)}
        actions = self._actions
        for planned, step in the_plan.steps():
            if actions is None:  # demo plans no steps; this only narrows the type
                break
            i = index[id(planned)]
            try:
                result = await self._run_step(actions, step)
            except (ExchangeUnavailable, ReconcileActionError) as exc:
                result = ActionResult.FAILED
                extra.setdefault(i, {})["error"] = _describe(exc)
                if isinstance(step, PlaceStop):
                    refused.add(i)
            if results.get(i) is not ActionResult.FAILED:
                results[i] = result
        outcomes: list[FindingOutcome] = []
        for i, planned in enumerate(the_plan.planned):
            details = {**planned.details, **extra.get(i, {})}
            halts, flattens, action = planned.halts, planned.flattens, planned.action
            if i in refused and mode is Mode.ACT:
                # The replacement stop was refused: never leave the position naked, flatten and halt (AC-9).
                halts, flattens, action = True, True, MismatchAction.FLATTENED
                details["why"] = STOP_REFUSED
            result = results.get(i, ActionResult.OK)
            if flattens and mode is Mode.ACT and result is not ActionResult.FAILED:
                result = ActionResult.PENDING  # the kill switch closes it
            outcomes.append(FindingOutcome(planned.finding, action, result, halts, flattens, details))
        return outcomes

    async def _run_step(self, actions: Actions, step: Step) -> ActionResult:
        match step:
            case CancelOrder():
                await actions.cancel.cancel(step.order)
                return ActionResult.OK
            case MarkCanceled():
                actions.reports.mark_canceled(step.order)
                return ActionResult.OK
            case SyncPosition():
                actions.reports.sync_position(step.instrument_id, step.exchange)
                return ActionResult.OK
            case PlaceStop():
                if not await actions.executor.place_stop(step):
                    raise ReconcileActionError("replacement stop refused or not accepted in time")
                # Accepted: only now may the other stops go (never naked).
                for order in step.then_cancel:
                    await actions.cancel.cancel(order)
                return ActionResult.OK
            case CloseAtMarket():
                actions.executor.close(step.position_id)
                return ActionResult.PENDING

    async def _publish_view(self, view: ExchangeView | None) -> None:
        if self._publish is not None:
            await self._publish(exchange_snapshot_data(view, self.clock.timestamp_ns()))

    def _finish(self, row: ReconciliationRow, outcomes: Sequence[FindingOutcome]) -> None:
        outcome = row.outcome
        with self._lock:
            self._last_outcome = outcome
            if outcome is ReconcileOutcome.CLEAN:
                self._last_clean_ns = row.finished_ns
        if outcome is ReconcileOutcome.CLEAN:
            unknown_cleared = self._blocks.clear(BlockReason.EXCHANGE_UNKNOWN)
            if self._blocks.clear(BlockReason.RECONCILING) or unknown_cleared:
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
        self._report_findings(row, outcomes)

    def _report_findings(self, row: ReconciliationRow, outcomes: Iterable[FindingOutcome]) -> None:
        for done in outcomes:
            finding = done.finding
            details: dict[str, JsonValue] = {"trigger": str(row.trigger), **done.details}
            if finding.reason is not None:
                details["reason"] = finding.reason
            if finding.orders:
                details["orders"] = [_order_details(o) for o in finding.orders]
            if finding.position is not None:
                details["position_id"] = finding.position.position_id
                details["strategy_id"] = finding.position.strategy_id
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
                action=done.action,
                action_result=done.result,
                details=details,
            )
            key = f"{EVENT_KEY_PREFIX}{mismatch.id}"
            message = f"reconciliation found {finding.kind} on {finding.instrument_id}, {done.action}"
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
                        "action_result": str(mismatch.action_result),
                        "halts": done.halts,
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


@dataclass(frozen=True)
class FindingOutcome:
    """A finding with the action taken, its result, and whether it halts the trader."""

    finding: Finding
    action: MismatchAction
    result: ActionResult
    halts: bool
    flattens: bool
    details: Mapping[str, str]


def _confirmed(first: Comparison, second: Comparison) -> Comparison:
    """The second fetch's findings that the first one also found (AC-27)."""
    keys = {f.key for f in first.findings}
    return Comparison(tuple(f for f in second.findings if f.key in keys))


def _order_details(order: OrderView) -> JsonObject:
    return {
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "side": str(order.side),
        "order_type": order.order_type,
        "quantity": str(order.quantity),
        "trigger_price": None if order.trigger_price is None else str(order.trigger_price),
        "reduce_only": order.reduce_only,
    }


def _check_ended(task: asyncio.Task[None]) -> None:
    if task.cancelled() or task.exception() is None:
        return
    log.critical(
        "reconciliation check crashed, entries stay blocked",
        exc_info=task.exception(),
        extra={"fields": {"code": "reconcile_crashed"}},
    )
