"""``LossLimitActor``: feeds the pure loss limits and acts on them (spec 0010 AC-8 to AC-14).

At start it subscribes to position events (held in a buffer), blocks entries with
``loss_unknown``, and reads the seed from Postgres in a worker thread, retrying every 30 s until
it lands. Then it folds the buffer (keys already in the seed are skipped) and from then on folds
each ``PositionOpened`` and ``PositionClosed`` at once. It evaluates on every position event and on
a 1 s timer that runs flat or not: the timer reads the latest quote for the unrealized part, rolls
the day at UTC midnight, and clears blocks whose time is up. A trip asks the ``KillSwitch`` to
halt; ``trade_cap`` writes one ``limit_hit`` event a day.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.events import PositionClosed, PositionOpened

from trading_core.control.messages import format_utc
from trading_core.node.control import KillSwitch, Record
from trading_core.recording.enums import Env, RiskEventKind, Severity
from trading_core.recording.queries import LossSeed
from trading_core.recording.rows import JsonObject, RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem
from trading_core.risk.limits import (
    LimitVerdict,
    LossLimits,
    LossTally,
    PositionKey,
    evaluate,
    on_closed,
    on_opened,
    roll,
    tripped,
    utc_day,
)
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

SEED_RETRY_S: Final = 30.0
CENT: Final = Decimal("0.01")
_TICK_TIMER: Final = "loss_limits_tick"
_TICK: Final = timedelta(seconds=1)
# The blocks this actor owns; ``loss_unknown`` is handled apart, by the seed.
_LIMIT_BLOCKS: Final = (BlockReason.DAILY_LOSS_LIMIT, BlockReason.TRADE_CAP, BlockReason.LOSS_COOLDOWN)

type ReadSeed = Callable[[datetime, datetime], LossSeed]
"""Reads the seed for ``(day_start, total_since)``; it blocks, so it runs in a worker thread."""


def seeded_tally(seed: LossSeed, now: datetime, limits: LossLimits) -> LossTally:
    """The tally the seed gives: today already tripped when a daily halt was recorded today, or
    today's realized loss alone reaches the daily limit (a restart the same day blocks again, AC-9)."""
    return LossTally(
        day=utc_day(now),
        realized_today=seed.realized_today,
        realized_total=seed.realized_total,
        trades_today=len(seed.opened_today_keys),
        opened_keys=seed.opened_today_keys,
        closed_keys=seed.closed_keys,
        last_loss_closed_at=seed.last_loss_closed_at,
        daily_tripped=seed.daily_halt_today or -seed.realized_today >= limits.daily_usd,
    )


def _cents(value: Decimal) -> str:
    return str(value.quantize(CENT))


def _utc(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)


class LossLimitActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Keeps the loss figures current and turns them into halts and entry blocks (see the module docstring).

    ``record`` and ``run_id`` are the paper loop's (demo); without them (live) events are only logged.
    """

    def __init__(
        self,
        *,
        limits: LossLimits,
        env: Env,
        read_seed: ReadSeed,
        kill_switch: KillSwitch,
        blocks: EntryBlocks,
        record: Record | None = None,
        run_id: UUID | None = None,
        seed_retry_s: float = SEED_RETRY_S,
    ) -> None:
        super().__init__(ActorConfig(component_id="LossLimitActor"))
        self._limits = limits
        self._env = env
        self._read_seed = read_seed
        self._kill_switch = kill_switch
        self._blocks = blocks
        self._record = record
        self._run_id = run_id
        self._seed_retry_s = seed_retry_s
        self._lock = threading.Lock()
        self._tally: LossTally | None = None
        self._buffer: list[PositionOpened | PositionClosed] = []
        self._verdict: LimitVerdict | None = None
        self._trade_cap_noted: str | None = None
        self._task: asyncio.Task[None] | None = None

    # Read by the kill switch and the snapshot ----------------------------------------------

    @property
    def seeded(self) -> bool:
        """True once the seed landed; until then the figures are unknown and entries blocked."""
        with self._lock:
            return self._tally is not None

    @property
    def total_over_limit(self) -> bool:
        """True while the total loss (latest evaluation) is at or over its limit; resume refuses then."""
        with self._lock:
            verdict = self._verdict
        return verdict is not None and verdict.loss_total >= self._limits.total_usd

    def figures(self) -> tuple[LossTally | None, LimitVerdict | None]:
        """The current tally and latest verdict (None until seeded), for ``trader_state``."""
        with self._lock:
            return self._tally, self._verdict

    @property
    def limits(self) -> LossLimits:
        """The thresholds, published in ``trader_state`` so the dashboard holds no copy."""
        return self._limits

    def snapshot_fields(self) -> dict[str, object]:
        """The loss fields of ``trader_state`` (spec 0010 AC-16): money as decimal strings to the cent,
        the cooldown end as ISO 8601 UTC; the figures are null until the seed lands, the limits never."""
        tally, verdict = self.figures()
        limits = self._limits
        return {
            "loss_today_usd": None if verdict is None else _cents(verdict.loss_today),
            "loss_total_usd": None if verdict is None else _cents(verdict.loss_total),
            "daily_loss_limit_usd": _cents(limits.daily_usd),
            "total_loss_limit_usd": _cents(limits.total_usd),
            "trades_today": None if tally is None else tally.trades_today,
            "max_trades_per_day": limits.max_trades_per_day,
            "cooldown_until": None
            if verdict is None or verdict.cooldown_until is None
            else format_utc(verdict.cooldown_until),
        }

    # Lifecycle ------------------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe first (events are held), block until seeded, seed in a thread, start the 1 s timer."""
        self.msgbus.subscribe(topic="events.position.*", handler=self._on_position_event)
        self._blocks.add(BlockReason.LOSS_UNKNOWN)
        self._task = asyncio.get_running_loop().create_task(self._seed_until_landed())
        self._task.add_done_callback(_seed_task_ended)
        self.clock.set_timer(_TICK_TIMER, _TICK, callback=lambda _event: self._evaluate())

    def on_stop(self) -> None:
        """Stop the seed retries and the timer."""
        if self._task is not None:
            self._task.cancel()
        if _TICK_TIMER in self.clock.timer_names:
            self.clock.cancel_timer(_TICK_TIMER)

    async def _seed_until_landed(self) -> None:
        while True:
            now = self.clock.utc_now()
            day_start = datetime.combine(utc_day(now), time(), tzinfo=UTC)
            total_since = datetime.combine(self._limits.total_since, time(), tzinfo=UTC)
            try:
                seed = await asyncio.to_thread(self._read_seed, day_start, total_since)
            except Exception as exc:
                log.warning(
                    "loss figures not read, entries stay blocked",
                    extra={"fields": {"code": "loss_seed_failed", "error": type(exc).__name__}},
                )
                await asyncio.sleep(self._seed_retry_s)
                continue
            self._land(seed, now)
            return

    def _land(self, seed: LossSeed, now: datetime) -> None:
        with self._lock:
            tally = roll(seeded_tally(seed, now, self._limits), self.clock.utc_now())
            for event in self._buffer:
                tally = self._fold(tally, event)
            self._buffer.clear()
            self._tally = tally
        self._blocks.clear(BlockReason.LOSS_UNKNOWN)
        log.info(
            "loss figures seeded",
            extra={
                "fields": {
                    "code": "loss_seeded",
                    "realized_today": str(tally.realized_today),
                    "realized_total": str(tally.realized_total),
                    "trades_today": tally.trades_today,
                    "daily_tripped": tally.daily_tripped,
                }
            },
        )
        self._evaluate()

    # Events ---------------------------------------------------------------------------------

    def _on_position_event(self, event: object) -> None:
        if not isinstance(event, PositionOpened | PositionClosed):
            return
        with self._lock:
            if self._tally is None:
                self._buffer.append(event)
                return
            # Roll first: an event between midnight and the next tick belongs to the new day.
            self._tally = self._fold(roll(self._tally, self.clock.utc_now()), event)
        self._evaluate()

    def _fold(self, tally: LossTally, event: PositionOpened | PositionClosed) -> LossTally:
        key: PositionKey = (event.position_id.value, event.ts_opened)
        if isinstance(event, PositionOpened):
            return on_opened(tally, key)
        tally = on_opened(tally, key)  # a position seen only when it closed still counts as a trade
        realized = Decimal(0) if event.realized_pnl is None else event.realized_pnl.as_decimal()
        return on_closed(tally, key, realized, _utc(event.ts_closed), self._limits)

    # Evaluation -----------------------------------------------------------------------------

    def _evaluate(self) -> None:
        now = self.clock.utc_now()
        unrealized = self._unrealized()
        with self._lock:
            if self._tally is None:
                return
            tally = roll(self._tally, now)
            verdict = evaluate(tally, unrealized, self._limits, now)
            if verdict.trip is not None:
                tally = tripped(tally, verdict.trip)
            self._tally = tally
            self._verdict = verdict
        if verdict.trip is not None:
            self._trip(verdict)
        self._apply_blocks(verdict, tally)

    def _unrealized(self) -> Decimal:
        """The open positions' unrealized P&L at the latest mid; a position with no quote yet counts 0."""
        total = Decimal(0)
        for position in self.cache.positions_open():
            quote = self.cache.quote_tick(position.instrument_id)
            instrument = self.cache.instrument(position.instrument_id)
            if quote is None or instrument is None:
                continue
            mid = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
            total += position.unrealized_pnl(instrument.make_price(mid)).as_decimal()
        return total

    def _trip(self, verdict: LimitVerdict) -> None:
        reason = verdict.trip
        assert reason is not None
        halt_id = self._kill_switch.halt_automatic(reason)
        log.critical(
            "loss limit reached, halting",
            extra={
                "fields": {
                    "code": "loss_limit_halt",
                    "reason": reason.value,
                    "halt_id": str(halt_id),
                    "loss_today": str(verdict.loss_today),
                    "loss_total": str(verdict.loss_total),
                }
            },
        )

    def _apply_blocks(self, verdict: LimitVerdict, tally: LossTally) -> None:
        for reason in _LIMIT_BLOCKS:
            if reason in verdict.blocks:
                if self._blocks.add(reason):
                    log.info("entries blocked", extra={"fields": {"code": "entries_blocked", "reason": reason.value}})
            elif self._blocks.clear(reason):
                log.info("entry block cleared", extra={"fields": {"code": "entries_unblocked", "reason": reason.value}})
        day = tally.day.isoformat()
        if BlockReason.TRADE_CAP in verdict.blocks and self._trade_cap_noted != day:
            self._trade_cap_noted = day
            self._event(
                f"trade_cap:{day}",
                RiskEventKind.LIMIT_HIT,
                Severity.WARNING,
                f"trade cap of {self._limits.max_trades_per_day} reached; entries blocked until midnight UTC",
                {"trades_today": tally.trades_today, "max_trades_per_day": self._limits.max_trades_per_day},
            )

    def _event(self, key: str, kind: RiskEventKind, severity: Severity, message: str, details: JsonObject) -> None:
        log.log(logging.WARNING, message, extra={"fields": {"code": kind.value, "event_key": key}})
        if self._record is None:
            return
        row = RiskEventRow(
            env=self._env,
            event_key=key,
            kind=kind,
            severity=severity,
            message=message,
            details=details,
            run_id=self._run_id,
            occurred_ns=self.clock.timestamp_ns(),
        )
        self._record(WriteItem(WriteCall.RECORD_RISK_EVENT, row))


def _seed_task_ended(task: asyncio.Task[None]) -> None:
    if task.cancelled() or task.exception() is None:
        return
    log.critical(
        "loss seed task crashed, entries stay blocked",
        exc_info=task.exception(),
        extra={"fields": {"code": "loss_seed_crashed"}},
    )
