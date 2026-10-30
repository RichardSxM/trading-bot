"""The one Redis poller per dashboard process (spec 0004, Live poller).

While at least one SSE subscriber exists it reads the trader heartbeat, ``trader_state`` (the
pause, spec 0005 AC-13), and the newest control commands (plus their results until each is
final) every ``DASHBOARD_POLL_MS``, renders the top bar partials, its alert bar (AC-21), and
Overview's Activity (AC-20), why entries are blocked (spec 0006 AC-18), and publishes each to every
subscriber only when its HTML changed. With no subscriber it idles. A Redis error renders every
partial ``no data`` and the next tick tries again.

With a position renderer (spec 0006 AC-17) it also reads the ``position`` and ``account``
snapshots every tick, and the P&L figures from Postgres at most every ``pnl_refresh_s``, off
the event loop. A Postgres error keeps the live card and shows the figures as unavailable. With an
exchange reader the card also shows Wanted, Trader, and Kraken (spec 0023 AC-18), the Kraken view
unknown once older than three ``check_interval_s``.

With an alerts reader (spec 0013 AC-16) the Activity feed also lists the last 24 h of alert
episodes, read from Postgres at most every ``alerts_refresh_s``; a Postgres error keeps the
commands and says the alerts are unavailable. With a mute renderer it reads the newest alert
command results every tick for the mute badge.
"""

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Final, Protocol

from redis.exceptions import RedisError

from dashboard.data.alerts import AlertLine
from dashboard.data.pnl import PnlFigures
from dashboard.live.alerts import MUTE_OFF, MuteView, merge_activity, mute_view
from dashboard.live.position import LOST, PositionView, position_view
from dashboard.live.sides import sides_view
from dashboard.live.state import (
    COMMAND_NO_DATA,
    LOSSES_NO_DATA,
    NO_DATA,
    NOT_BLOCKED,
    PAUSE_NO_DATA,
    RECONCILE_NO_DATA,
    ActivityRow,
    AgeView,
    AlertView,
    BlockedView,
    CommandView,
    Entry,
    LossesView,
    PauseView,
    ReconcileView,
    activity_row,
    alert_state,
    blocked_view,
    command_state,
    find_result,
    heartbeat_view,
    losses_view,
    pause_view,
    reconcile_view,
)
from trading_core.control.messages import ControlMessage, Fields, field, to_fields

log = logging.getLogger(__name__)

AGE_EVENT: Final = "topbar-age"
COMMAND_EVENT: Final = "topbar-command"
ALERT_EVENT: Final = "topbar-alert"
ACTIVITY_EVENT: Final = "overview-activity"
PAUSE_EVENT: Final = "topbar-pause"
POSITION_EVENT: Final = "overview-position"
BLOCKED_EVENT: Final = "topbar-blocked"
BLOCKED_REASONS_EVENT: Final = "overview-blocked"
LOSSES_EVENT: Final = "system-losses"
CHECK_EVENT: Final = "topbar-check"
MUTE_EVENT: Final = "overview-mute"
ALERTS_REFRESH_S: Final = 10.0
PNL_REFRESH_S: Final = 5.0
PNL_TIMEOUT_S: Final = 2.0
PNL_WARN_EVERY_S: Final = 60.0
ACTIVITY_SIZE: Final = 20
REDIS_ERRORS: Final = (RedisError, OSError, TimeoutError)
QUEUE_SIZE: Final = 8
RESTART_AFTER_S: Final = 1.0
SKEW_WARN_EVERY_S: Final = 60.0

Event = tuple[str, str]


class Poller:
    """Reads live values, renders them, and fans changes out to subscriber queues."""

    def __init__(
        self,
        read_heartbeat: Callable[[], Awaitable[str | bytes | None]],
        render_age: Callable[[AgeView], str],
        interval_s: float,
        amber_s: float,
        red_s: float,
        *,
        read_command: Callable[[], Awaitable[Sequence[Entry] | None]],
        read_results: Callable[[], Awaitable[Sequence[Entry] | None]],
        render_command: Callable[[CommandView], str],
        render_alert: Callable[[AlertView | None], str],
        render_activity: Callable[[Sequence[ActivityRow] | None], str],
        read_trader_state: Callable[[], Awaitable[str | bytes | None]],
        render_pause: Callable[[PauseView], str],
        hide_after_s: float,
        ack_warn_s: float,
        read_timeout_s: float,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        read_position: Callable[[], Awaitable[str | bytes | None]] | None = None,
        read_account: Callable[[], Awaitable[str | bytes | None]] | None = None,
        read_pnl: Callable[[], Awaitable[PnlFigures]] | None = None,
        render_position: Callable[[PositionView], str] | None = None,
        render_blocked: Callable[[BlockedView], str] | None = None,
        render_blocked_reasons: Callable[[BlockedView], str] | None = None,
        render_losses: Callable[[LossesView], str] | None = None,
        pnl_refresh_s: float = PNL_REFRESH_S,
        pnl_timeout_s: float = PNL_TIMEOUT_S,
        read_alerts: Callable[[], Awaitable[Sequence[AlertLine]]] | None = None,
        read_alert_results: Callable[[], Awaitable[Sequence[Entry] | None]] | None = None,
        render_mute: Callable[[MuteView], str] | None = None,
        alerts_refresh_s: float = ALERTS_REFRESH_S,
        render_check: Callable[[ReconcileView], str] | None = None,
        read_exchange: Callable[[], Awaitable[str | bytes | None]] | None = None,
        check_interval_s: float = 60.0,
    ) -> None:
        self._read_exchange = read_exchange
        self._check_interval_s = check_interval_s
        self._render_check = render_check
        self._read_alerts = read_alerts
        self._read_alert_results = read_alert_results
        self._render_mute = render_mute
        self._alerts_refresh_s = alerts_refresh_s
        self._alerts: Sequence[AlertLine] | None = ()
        self._alerts_read_at: float | None = None
        self._last_alerts_warning: float | None = None
        self._read_heartbeat = read_heartbeat
        self._read_position = read_position
        self._read_account = read_account
        self._read_pnl = read_pnl
        self._render_position = render_position
        self._render_blocked = render_blocked
        self._render_blocked_reasons = render_blocked_reasons
        self._render_losses = render_losses
        self._pnl_refresh_s = pnl_refresh_s
        self._pnl_timeout_s = pnl_timeout_s
        self._pnl: PnlFigures | None = None
        self._pnl_read_at: float | None = None
        self._last_pnl_warning: float | None = None
        self._read_timeout_s = read_timeout_s
        self._render_age = render_age
        self._read_command = read_command
        self._read_results = read_results
        self._render_command = render_command
        self._render_alert = render_alert
        self._render_activity = render_activity
        self._read_trader_state = read_trader_state
        self._render_pause = render_pause
        self._hide_after_s = hide_after_s
        self._ack_warn_s = ack_warn_s
        self._interval_s = interval_s
        self._amber_s = amber_s
        self._red_s = red_s
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._wake = asyncio.Event()
        self._cache: dict[str, str] = {}
        self._last_skew_warning: float | None = None
        # The latest age, so a stop sent between ticks can update the alert bar at once.
        self._age: AgeView | None = None
        # Stream id to final result for the commands in view: no need to read those results again.
        self._finals: dict[str | bytes | None, Fields] = {}
        self._warned_invalid: set[tuple[object, ...]] = set()

    @property
    def cached(self) -> dict[str, str]:
        """The latest rendered partial per event name."""
        return dict(self._cache)

    def subscribe(self) -> asyncio.Queue[Event]:
        """A new bounded queue that receives every changed partial; wakes an idle poller."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        self._wake.set()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Stop sending to ``queue``."""
        self._subscribers.discard(queue)

    async def render_now(self) -> dict[str, str]:
        """Read Redis and render every partial without publishing (for a full page render).

        All reads share one ``read_timeout_s`` deadline, so a down or hung Redis renders ``link lost``
        within it instead of waiting out redis-py's retries on every read (AC-13).
        """
        try:
            raw, state, (command, rows) = await asyncio.wait_for(self._read_all(), timeout=self._read_timeout_s)
            snapshots = await asyncio.wait_for(self._read_snapshots(), timeout=self._read_timeout_s)
            alert_results = await asyncio.wait_for(self._read_mute(), timeout=self._read_timeout_s)
        except REDIS_ERRORS as exc:
            log.warning(
                "redis read failed", extra={"fields": {"code": "redis_read_failed", "error": type(exc).__name__}}
            )
            await self._refresh_pnl()
            return self.no_data()
        age = heartbeat_view(raw, self._clock_ns(), self._amber_s, self._red_s)
        if age.skewed:
            self._warn_skew()
        self._age = age
        alert = self._alert(age, command)
        if self._read_alerts is not None:
            await self._refresh_alerts()
            rows = merge_activity(rows, self._alerts)
        partials = {
            AGE_EVENT: self._render_age(age),
            COMMAND_EVENT: self._render_command(_tinted(command, alert)),
            ALERT_EVENT: self._render_alert(alert),
            ACTIVITY_EVENT: self._render_activity(rows),
            PAUSE_EVENT: self._render_pause(pause_view(state, self._now())),
            **self._blocked(blocked_view(state)),
        }
        if self._render_losses is not None:
            partials[LOSSES_EVENT] = self._render_losses(losses_view(state, self._now()))
        if self._render_check is not None:
            partials[CHECK_EVENT] = self._render_check(reconcile_view(state, self._now()))
        if self._render_mute is not None:
            partials[MUTE_EVENT] = self._render_mute(mute_view(alert_results, self._now()))
        if self._render_position is not None:
            await self._refresh_pnl()
            raw_position, raw_account, raw_exchange = snapshots
            view = position_view(raw_position, raw_account, self._pnl, self._clock_ns(), self._amber_s, self._red_s)
            if self._read_exchange is not None:
                sides = sides_view(
                    raw_position, raw_exchange, state, self._clock_ns(), self._red_s, self._check_interval_s
                )
                view = dataclasses.replace(view, sides=sides)
            partials[POSITION_EVENT] = self._render_position(view)
        return partials

    def no_data(self) -> dict[str, str]:
        """Every partial in its ``no data`` state (Redis unreadable), without reading Redis."""
        self._age = NO_DATA
        partials = {
            AGE_EVENT: self._render_age(NO_DATA),
            COMMAND_EVENT: self._render_command(COMMAND_NO_DATA),
            ALERT_EVENT: self._render_alert(self._alert(NO_DATA, COMMAND_NO_DATA)),
            ACTIVITY_EVENT: self._render_activity(None),
            PAUSE_EVENT: self._render_pause(PAUSE_NO_DATA),
            **self._blocked(NOT_BLOCKED),
        }
        if self._render_losses is not None:
            partials[LOSSES_EVENT] = self._render_losses(LOSSES_NO_DATA)
        if self._render_check is not None:
            partials[CHECK_EVENT] = self._render_check(RECONCILE_NO_DATA)
        if self._render_mute is not None:
            partials[MUTE_EVENT] = self._render_mute(MUTE_OFF)
        if self._render_position is not None:
            partials[POSITION_EVENT] = self._render_position(dataclasses.replace(LOST, pnl=self._pnl))
        return partials

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock_ns() / 1_000_000_000, tz=UTC)

    def _blocked(self, view: BlockedView) -> dict[str, str]:
        partials: dict[str, str] = {}
        if self._render_blocked is not None:
            partials[BLOCKED_EVENT] = self._render_blocked(view)
        if self._render_blocked_reasons is not None:
            partials[BLOCKED_REASONS_EVENT] = self._render_blocked_reasons(view)
        return partials

    async def _read_snapshots(self) -> tuple[str | bytes | None, str | bytes | None, str | bytes | None]:
        if self._render_position is None or self._read_position is None or self._read_account is None:
            return None, None, None
        exchange = None if self._read_exchange is None else await self._read_exchange()
        return await self._read_position(), await self._read_account(), exchange

    async def _read_mute(self) -> Sequence[Entry] | None:
        if self._render_mute is None or self._read_alert_results is None:
            return None
        return await self._read_alert_results()

    async def _refresh_alerts(self) -> None:
        """Read the alert episodes when the last read is older than ``alerts_refresh_s``; None on failure."""
        if self._read_alerts is None:
            return
        now = self._monotonic()
        if self._alerts_read_at is not None and now - self._alerts_read_at < self._alerts_refresh_s:
            return
        self._alerts_read_at = now
        try:
            self._alerts = await asyncio.wait_for(self._read_alerts(), timeout=self._pnl_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._alerts = None
            if self._last_alerts_warning is None or now - self._last_alerts_warning >= PNL_WARN_EVERY_S:
                self._last_alerts_warning = now
                log.warning(
                    "alerts read failed", extra={"fields": {"code": "alerts_read_failed", "error": type(exc).__name__}}
                )

    async def _refresh_pnl(self) -> None:
        """Read the P&L figures when the last read is older than ``pnl_refresh_s``; keep None on failure."""
        if self._read_pnl is None:
            return
        now = self._monotonic()
        if self._pnl_read_at is not None and now - self._pnl_read_at < self._pnl_refresh_s:
            return
        self._pnl_read_at = now
        try:
            self._pnl = await asyncio.wait_for(self._read_pnl(), timeout=self._pnl_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._pnl = None
            if self._last_pnl_warning is None or now - self._last_pnl_warning >= PNL_WARN_EVERY_S:
                self._last_pnl_warning = now
                log.warning(
                    "P&L read failed", extra={"fields": {"code": "pnl_read_failed", "error": type(exc).__name__}}
                )

    async def _read_all(
        self,
    ) -> tuple[str | bytes | None, str | bytes | None, tuple[CommandView, list[ActivityRow]]]:
        return await self._read_heartbeat(), await self._read_trader_state(), await self._commands()

    async def command_view(self) -> CommandView:
        """The latest command and its outcome; reads results only until one is final (AC-12)."""
        return (await self._commands())[0]

    async def _commands(self) -> tuple[CommandView, list[ActivityRow]]:
        """The top bar state and the Activity rows from one read of each stream (AC-12, AC-20).

        Results are read only while some command in view has no final outcome yet.
        """
        now = self._now()
        entries = [
            (stream_id, raw if raw is not None else {})  # a deleted entry parses as unknown
            for stream_id, raw in (await self._read_command() or ())
        ]
        open_ids = [field(command, "id") for stream_id, command in entries if stream_id not in self._finals]
        results = await self._read_results() if any(open_ids) else None
        finals: dict[str | bytes | None, Fields] = {}
        rows: list[ActivityRow] = []
        view = command_state(None, None, now, self._hide_after_s)
        for index, (stream_id, command) in enumerate(entries):
            result = self._finals.get(stream_id)
            if result is None:
                command_id = field(command, "id")
                result = None if command_id is None else find_result(results, command_id)
            row = activity_row(command, result, now)
            if index == 0:
                view = command_state(command, result, now, self._hide_after_s)
            if row.final and result is not None:
                finals[stream_id] = result
            if row.invalid or (index == 0 and view.invalid):
                self._warn_invalid((stream_id, None if result is None else tuple(result.items())))
            rows.append(row)
        self._finals = finals
        return view, rows

    def note_sent(self, msg: ControlMessage) -> None:
        """Show a command the dashboard just appended at once, in the cache and to every subscriber."""
        view = command_state(to_fields(msg), None, self._now(), self._hide_after_s)
        partials = {COMMAND_EVENT: self._render_command(view)}
        if self._age is not None:  # a new stop replaces the alert about the last one
            alert = self._alert(self._age, view)
            partials = {
                COMMAND_EVENT: self._render_command(_tinted(view, alert)),
                ALERT_EVENT: self._render_alert(alert),
            }
        for name, html in partials.items():
            if self._cache.get(name) != html:
                self._cache[name] = html
                self._publish((name, html))

    async def tick(self) -> None:
        """One poll: render each partial and publish the ones whose HTML changed."""
        for name, html in (await self.render_now()).items():
            if self._cache.get(name) != html:
                self._cache[name] = html
                self._publish((name, html))

    async def run(self) -> None:
        """Poll while anyone listens, idle otherwise; runs until cancelled."""
        while True:
            if not self._subscribers:
                self._wake.clear()
                await self._wake.wait()
            await self.tick()
            await asyncio.sleep(self._interval_s)

    def _publish(self, event: Event) -> None:
        for queue in self._subscribers:
            if queue.full():  # a slow browser loses the oldest update, never blocks the others
                queue.get_nowait()
            queue.put_nowait(event)

    def _alert(self, age: AgeView, command: CommandView) -> AlertView | None:
        return alert_state(age, command, self._now(), self._ack_warn_s)

    def _warn_invalid(self, key: tuple[object, ...]) -> None:
        if key not in self._warned_invalid:
            if len(self._warned_invalid) >= 4 * ACTIVITY_SIZE:  # bounded; an old entry may warn again
                self._warned_invalid.clear()
            self._warned_invalid.add(key)
            log.warning(
                "control entry does not parse, showing unknown result",
                extra={"fields": {"code": "control_result_invalid"}},
            )

    def _warn_skew(self) -> None:
        now = self._monotonic()
        if self._last_skew_warning is None or now - self._last_skew_warning >= SKEW_WARN_EVERY_S:
            self._last_skew_warning = now
            log.warning("heartbeat is from the future, showing 0s", extra={"fields": {"code": "clock_skew"}})


def _tinted(command: CommandView, alert: AlertView | None) -> CommandView:
    """The command state in amber while the alert bar warns it is not acknowledged (AC-21)."""
    if alert is not None and alert.kind == "stop_unacknowledged":
        return dataclasses.replace(command, level="warn")
    return command


class Runnable(Protocol):
    """Anything with a long running ``run`` coroutine (the ``Poller``)."""

    async def run(self) -> None: ...


async def supervise(poller: Runnable, restart_after_s: float = RESTART_AFTER_S) -> None:
    """Run ``poller`` forever; a crash logs ERROR ``poller_crashed`` and restarts it after a pause."""
    while True:
        try:
            await poller.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poller crashed, restarting", extra={"fields": {"code": "poller_crashed"}})
            await asyncio.sleep(restart_after_s)
