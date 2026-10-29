"""``ReconcileExecutor``: the thin strategy that sends the reconciliation check's orders (spec 0023 AC-7, AC-9).

An Actor cannot submit orders, and no other strategy owns a position the check must close, so the
``ReconcileActor`` runs its order steps through this strategy. It sends only reduce only orders:

- ``place_stop``: the target stop for a held position, tagged ``origin:reconciliation`` and
  ``parent:<opening order>``, submitted against that position so its fill closes it. Resolves True
  once Kraken accepts it, False on a denial, a rejection, or no answer within ``accept_timeout_s``.
- ``close``: a reduce only market order for one position (a stop whose trigger is already crossed).
- ``flatten``: part of the kill switch's flatten. It closes every open position no loaded strategy
  owns (``EXTERNAL`` ones Nautilus added during reconciliation, or one of a strategy that is not
  loaded), tagged ``origin:kill_switch`` and ``halt:<halt_id>``.

A close that is denied or rejected is sent again every second while its position is open, like the
exit loop of spec 0010. A close already in flight for a position is never sent twice.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Final

from nautilus_trader.model.enums import OrderSide as NautilusOrderSide
from nautilus_trader.model.enums import PositionSide as NautilusPositionSide
from nautilus_trader.model.enums import TimeInForce, TriggerType
from nautilus_trader.model.events import OrderAccepted, OrderDenied, OrderRejected
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId
from nautilus_trader.model.position import Position

from trading_core.node.strategy import KfsStrategy, KfsStrategyConfig, PauseGate
from trading_core.reconcile.plan import PlaceStop
from trading_core.recording.enums import OrderOrigin, OrderSide
from trading_core.recording.mappers import HALT_TAG, ORIGIN_TAG, PARENT_TAG

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "reconcile_executor"
# How long Kraken gets to accept a replacement stop before it counts as rejected (AC-9).
ACCEPT_TIMEOUT_S: Final = 10.0
CLOSE_RETRY_NS: Final = 1_000_000_000
_ORIGIN: Final = f"{ORIGIN_TAG}{OrderOrigin.RECONCILIATION}"


class ReconcileExecutorConfig(KfsStrategyConfig, frozen=True, kw_only=True):
    """The executor's config; ``instrument_id`` is only the base class's, it acts on any instrument."""

    accept_timeout_s: float = ACCEPT_TIMEOUT_S


class ReconcileExecutor(KfsStrategy):
    """Sends the check's stops and closes (see the module docstring).

    ``loaded`` returns the ids of the loaded strategies; a position none of the others owns is this
    executor's to close on a flatten.
    """

    def __init__(self, config: ReconcileExecutorConfig, gate: PauseGate, loaded: Callable[[], frozenset[str]]) -> None:
        super().__init__(config, gate)
        self._accept_timeout_s = config.accept_timeout_s
        self._loaded = loaded
        self._accepts: dict[ClientOrderId, asyncio.Future[bool]] = {}
        self._closes: dict[PositionId, ClientOrderId] = {}
        self._close_tags: dict[PositionId, list[str]] = {}

    # Stops ----------------------------------------------------------------------------------

    async def place_stop(self, step: PlaceStop) -> bool:
        """Place the target stop; True once Kraken accepted it."""
        instrument = self.cache.instrument(InstrumentId.from_str(step.instrument_id))
        if instrument is None:
            log.error(
                "replacement stop not placed, instrument not in the cache",
                extra={"fields": {"code": "reconcile_stop_no_instrument", "instrument_id": step.instrument_id}},
            )
            return False
        tags = [_ORIGIN]
        if step.parent_client_order_id is not None:
            tags.append(f"{PARENT_TAG}{step.parent_client_order_id}")
        order = self.order_factory.stop_market(
            instrument.id,
            NautilusOrderSide.SELL if step.side is OrderSide.SELL else NautilusOrderSide.BUY,
            instrument.make_qty(step.quantity),
            trigger_price=instrument.make_price(step.trigger_price),
            trigger_type=TriggerType.DEFAULT,
            reduce_only=True,
            time_in_force=TimeInForce.GTC,
            tags=tags,
        )
        accepted: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._accepts[order.client_order_id] = accepted
        try:
            self.submit_order(order, position_id=PositionId(step.position_id))
            async with asyncio.timeout(self._accept_timeout_s):
                return await accepted
        except TimeoutError:
            log.error(
                "replacement stop not accepted in time",
                extra={"fields": {"code": "reconcile_stop_timeout", "client_order_id": str(order.client_order_id)}},
            )
            return False
        finally:
            self._accepts.pop(order.client_order_id, None)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """Resolve a waiting stop as accepted."""
        self._resolve(event.client_order_id, True)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """Resolve a waiting stop as refused, or retry a refused close."""
        self._refused(event.client_order_id, f"rejected: {event.reason}")

    def on_order_denied(self, event: OrderDenied) -> None:
        """Resolve a waiting stop as refused, or retry a refused close."""
        self._refused(event.client_order_id, f"denied: {event.reason}")

    def _resolve(self, client_order_id: ClientOrderId, accepted: bool) -> None:
        future = self._accepts.get(client_order_id)
        if future is not None and not future.done():
            future.set_result(accepted)

    def _refused(self, client_order_id: ClientOrderId, why: str) -> None:
        self._resolve(client_order_id, False)
        for position_id, close_id in list(self._closes.items()):
            if close_id != client_order_id:
                continue
            del self._closes[position_id]
            log.warning(
                "reconciliation close refused, retrying in 1 s",
                extra={"fields": {"code": "reconcile_close_refused", "position_id": str(position_id), "why": why}},
            )
            self.clock.set_time_alert_ns(
                f"reconcile_close_retry:{position_id}:{self.clock.timestamp_ns()}",
                self.clock.timestamp_ns() + CLOSE_RETRY_NS,
                lambda _event, pid=position_id: self._retry_close(pid),
            )

    # Closes ---------------------------------------------------------------------------------

    def close(self, position_id: str) -> None:
        """Close one position at market, reduce only (its stop's trigger is already crossed)."""
        position = self.cache.position(PositionId(position_id))
        if position is not None and position.is_open:
            self._close(position, [_ORIGIN])

    def flatten(self, halt_id: uuid.UUID) -> None:
        """Close every open position no loaded strategy owns, for the kill switch (idempotent)."""
        tags = [f"{ORIGIN_TAG}{OrderOrigin.KILL_SWITCH}", f"{HALT_TAG}{halt_id}"]
        for position in self._orphans():
            self._close(position, tags)

    def has_open_positions(self) -> bool:
        """True while a position no loaded strategy owns is open."""
        return bool(self._orphans())

    def _orphans(self) -> list[Position]:
        others = self._loaded() - {str(self.id)}
        return [p for p in self.cache.positions_open() if str(p.strategy_id) not in others]

    def _retry_close(self, position_id: PositionId) -> None:
        position = self.cache.position(position_id)
        if position is not None and position.is_open:
            self._close(position, self._close_tags.get(position_id, [_ORIGIN]))

    def _close(self, position: Position, tags: list[str]) -> None:
        in_flight = self._closes.get(position.id)
        if in_flight is not None:
            order = self.cache.order(in_flight)
            if order is not None and not order.is_closed:
                return
        order = self.order_factory.market(
            position.instrument_id,
            NautilusOrderSide.SELL if position.side == NautilusPositionSide.LONG else NautilusOrderSide.BUY,
            position.quantity,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,
        )
        self._closes[position.id] = order.client_order_id
        self._close_tags[position.id] = tags
        log.warning(
            "reconciliation closing a position",
            extra={
                "fields": {
                    "code": "reconcile_close",
                    "position_id": str(position.id),
                    "strategy_id": str(position.strategy_id),
                    "client_order_id": str(order.client_order_id),
                }
            },
        )
        self.submit_order(order, position_id=position.id)
