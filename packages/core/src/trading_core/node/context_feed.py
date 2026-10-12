"""The raw public Kraken context feed: open interest and liquidations (spec 0009 AC-3 to AC-6, AC-12).

NautilusTrader 1.231.0's Kraken adapter offers neither, so ``ContextFeed`` reads Kraken's public
futures websocket itself (``ticker`` and ``trade``), with the ``websockets`` library, as a task on
the node's event loop. It sends no credentials. ``ContextFeedActor`` wraps it: it turns each record
into an ``OpenInterestUpdate`` or ``LiquidationTick`` at the instrument's precision and publishes it
on the message bus, where the streaming writer and any subscriber receive it.

Nothing the feed does can stop the node: every failure is logged and the loop carries on, with a
reconnect after a drop, an error, or ``idle_s`` without any message. The connection is
``connecting``, then ``subscribed`` (first message), then ``down`` after a drop, and so on; the
outage from the last message before a drop to the first after it is logged and, in the recorder,
appended to ``context_outages.jsonl`` for the compactor's ``reconnect`` gaps.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.identifiers import InstrumentId
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from trading_core.marketdata.context_feed import (
    BACKOFF_RESET_S,
    BadCounter,
    BadRow,
    ForcedTrade,
    OpenInterest,
    Outage,
    RecentIds,
    backoff_s,
    parse_message,
    product_id,
    subscribe_messages,
)
from trading_core.marketdata.hours import SECOND_NS
from trading_core.node.context_types import LiquidationTick, OpenInterestUpdate, context_data_type

log = logging.getLogger(__name__)

PING_INTERVAL_S: Final = 20
PING_TIMEOUT_S: Final = 20
OPEN_TIMEOUT_S: Final = 10

type Record = OpenInterest | ForcedTrade
# Publishes one record received at ``ts_init``; returns why it was skipped, or None when published.
type Emit = Callable[[Record, int], str | None]
type Connect = Callable[..., Any]


class ContextFeed:
    """The websocket loop of the context feed, apart from Nautilus (``ContextFeedActor`` runs it).

    ``clock_ns`` stamps every message's ``ts_init``; ``outages_path`` gives the outages file (None in
    the trader, which records nothing). ``connect`` and ``sleep`` are the real ones outside tests.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        product_ids: list[str],
        reconnect_max_s: int,
        idle_s: float,
        clock_ns: Callable[[], int],
        emit: Emit,
        outages_path: Callable[[], Path] | None = None,
        connect: Connect = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = ws_url
        self._products = product_ids
        self._reconnect_max_s = reconnect_max_s
        self._idle_s = idle_s
        self._clock_ns = clock_ns
        self._emit = emit
        self._outages_path = outages_path
        self._connect = connect
        self._sleep = sleep
        self._recent = RecentIds()
        self._bad = BadCounter()
        self._attempt = 0
        self._last_ns: int | None = None
        self._down_reason: str | None = None
        self.state = "connecting"
        self.outages: list[Outage] = []

    async def run(self) -> None:
        """Connect, read, and reconnect until cancelled."""
        while True:
            reason, connected_s = await self._session()
            self._went_down(reason)
            if connected_s >= BACKOFF_RESET_S:
                self._attempt = 0
            wait = backoff_s(self._attempt, self._reconnect_max_s)
            self._attempt += 1
            await self._sleep(wait)

    async def _session(self) -> tuple[str, float]:
        """One connection, until it drops; returns why, and how long it was connected."""
        opened: float | None = None
        try:
            async with self._connect(
                self._url,
                ping_interval=PING_INTERVAL_S,
                ping_timeout=PING_TIMEOUT_S,
                open_timeout=OPEN_TIMEOUT_S,
            ) as ws:
                opened = time.monotonic()
                for message in subscribe_messages(self._products):
                    await ws.send(message)
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._idle_s)
                    self._received(raw)
        except TimeoutError:
            reason = "idle" if opened is not None else "connect_timeout"
        except ConnectionClosed:
            reason = "closed"
        except (OSError, InvalidHandshake, InvalidURI) as exc:
            reason = type(exc).__name__
        except Exception:
            log.exception("context feed failed", extra={"fields": {"code": "context_feed_error"}})
            reason = "error"
        return reason, 0.0 if opened is None else time.monotonic() - opened

    def _went_down(self, reason: str) -> None:
        if self._down_reason is not None:
            return  # one log per outage, however many reconnects fail
        self._down_reason = reason
        self.state = "down"
        log.warning(
            "context feed down",
            extra={"fields": {"code": "context_feed_down", "reason": reason, "last_message_ns": self._last_ns}},
        )

    def _came_up(self, now_ns: int) -> None:
        reason, self._down_reason = self._down_reason, None
        self.state = "subscribed"
        if reason is None:
            return  # the first connection
        outage_s = None
        if self._last_ns is not None:
            outage = Outage(self._last_ns, now_ns, reason)
            outage_s = round((now_ns - self._last_ns) / SECOND_NS, 3)
            self.outages.append(outage)
            self._append(outage)
        log.warning(
            "context feed up",
            extra={"fields": {"code": "context_feed_up", "reason": reason, "outage_s": outage_s}},
        )

    def _received(self, raw: str | bytes) -> None:
        now_ns = self._clock_ns()
        if self.state != "subscribed":
            self._came_up(now_ns)
        self._last_ns = now_ns
        try:
            parsed = parse_message(raw)
            for bad in parsed.bad:
                self._count(bad, now_ns)
            for record in parsed.records:
                if isinstance(record, ForcedTrade) and not self._recent.add(record.trade_id):
                    continue  # a resubscribe snapshot resends recent trades (AC-5)
                skipped = self._emit(record, now_ns)
                if skipped is not None:
                    self._count(BadRow("ticker" if isinstance(record, OpenInterest) else "trade", skipped), now_ns)
        except Exception:
            # Nothing raised here may end the feed (AC-4).
            if self._bad.add(BadRow("unknown", "handler_error"), now_ns) is not None:
                log.exception(
                    "context message failed",
                    extra={"fields": {"code": "context_message_bad", "reason": "handler_error"}},
                )

    def _count(self, bad: BadRow, now_ns: int) -> None:
        count = self._bad.add(bad, now_ns)
        if count is not None:
            log.warning(
                "context message skipped",
                extra={
                    "fields": {"code": "context_message_bad", "feed": bad.feed, "reason": bad.reason, "count": count}
                },
            )

    def _append(self, outage: Outage) -> None:
        if self._outages_path is None:
            return
        try:
            path = self._outages_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                data = outage.line().encode()
                while data:
                    data = data[os.write(fd, data) :]
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            # Only logged: no context failure touches the recorder (AC-6).
            log.error(
                "context outage not written",
                extra={"fields": {"code": "context_outage_write_failed", "error": str(exc)}},
            )


class ContextFeedConfig(ActorConfig, frozen=True):
    """The feed's settings (``ContextSettings``); ``instrument_ids`` are Nautilus ids such as ``PF_XBTUSD.KRAKEN``."""

    ws_url: str
    instrument_ids: list[str]
    reconnect_max_s: int = 60
    idle_s: int = 30


class ContextFeedActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Runs ``ContextFeed`` on the node's event loop and publishes what it reads (AC-3).

    ``outages_path`` gives the run's ``context_outages.jsonl`` in the recorder, and is None in the
    trader (AC-12). A message for an instrument not in the cache yet is skipped as ``instrument_not_loaded``.
    """

    def __init__(self, config: ContextFeedConfig, outages_path: Callable[[], Path] | None = None) -> None:
        super().__init__(config)
        self._ids = {product_id(i): InstrumentId.from_str(i) for i in config.instrument_ids}
        self._config = config
        self._outages_path = outages_path
        self._task: asyncio.Task[None] | None = None
        self.feed: ContextFeed | None = None

    def on_start(self) -> None:
        """Start the feed as a task on the running loop."""
        self.feed = ContextFeed(
            ws_url=self._config.ws_url,
            product_ids=list(self._ids),
            reconnect_max_s=self._config.reconnect_max_s,
            idle_s=self._config.idle_s,
            clock_ns=self.clock.timestamp_ns,
            emit=self._publish,
            outages_path=self._outages_path,
        )
        self._task = asyncio.get_running_loop().create_task(self.feed.run(), name="context_feed")
        self._task.add_done_callback(_log_end)

    def on_stop(self) -> None:
        """Stop the feed."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _publish(self, record: Record, ts_init: int) -> str | None:
        instrument_id = self._ids.get(record.product_id)
        if instrument_id is None:
            return "unknown_product"
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return "instrument_not_loaded"
        if isinstance(record, OpenInterest):
            update = OpenInterestUpdate(instrument_id, instrument.make_qty(record.value), record.ts_event, ts_init)
            self.publish_data(context_data_type(OpenInterestUpdate, instrument_id), update)
            return None
        tick = LiquidationTick(
            instrument_id,
            record.trade_id,
            record.kind,
            record.aggressor_side,
            instrument.make_price(record.price),
            instrument.make_qty(record.size),
            record.ts_event,
            ts_init,
        )
        self.publish_data(context_data_type(LiquidationTick, instrument_id), tick)
        return None


def _log_end(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "context feed ended", extra={"fields": {"code": "context_feed_error", "error": repr(error)}}, exc_info=error
        )
