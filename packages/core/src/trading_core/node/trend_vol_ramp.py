"""``TrendVolRamp``: ``trend_vol`` that builds a position over three daily runs, with a wide stop (spec 0034).

Everything ``TrendVol`` does is inherited unchanged: book volatility sizing, the band, the schedule, orders,
flips, retries, decisions, seeds, and the start and resume rebalances. In addition (AC-3 to AC-7):

- Each round trades toward a working plan built from ``full_plan`` (the sized plan) with
  ``trading_core.strategy.trend_vol_ramp.ramp_target``: a new or flipped position grows by a third per round,
  only the daily run may grow, and a cut goes to the full target at once. A round that is not daily skips
  every coin it would only hold, so price rounding never sends a stray cut.
- Every full hour, a position that moved ``stop_vols`` entry vols against its average entry price is closed
  reduce only (a ``stop_vol`` decision) and the coin stays flat until a daily plan's signal leaves the
  stopped side. Stops are rebuilt from the recorded decisions after a restart (``stops``).
"""

import dataclasses
import functools
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from trading_core.node import guard
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_daily import DAILY
from trading_core.node.trend_portfolio import _RETRY_PREFIX, NS_PER_S, Record
from trading_core.node.trend_vol import TrendVol, TrendVolConfig
from trading_core.recording.enums import Intent
from trading_core.recording.mappers import DECISION_TAG
from trading_core.recording.rows import JsonObject, JsonValue
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import (
    CoinPlan,
    DailyClose,
    PortfolioPlan,
    portfolio_plan,
    sample_vol,
    target_quantity,
)
from trading_core.strategy.trend_vol_ramp import RampParams, ramp_target, stop_hit, stop_holds

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "trend_vol_ramp"
ORDER_ID_TAG: Final = "TVR"
STOP_REASON: Final = "stop_vol"
HOUR_NS: Final = 3600 * NS_PER_S
BUILDING_BELOW_PCT: Final = Decimal(70)

_STOP_CHECK: Final = "tvr_stop_check"
_STOP_RETRY_PREFIX: Final = "tvr_stop_retry:"
_CENT: Final = Decimal("0.01")

type Stops = Mapping[str, tuple[int, int]]
"""Recorded stops by coin: (``decided_ns`` of the newest ``stop_vol`` decision, the stopped side +1 or -1)."""


class TrendVolRampConfig(TrendVolConfig, frozen=True, kw_only=True):
    """``TrendVolConfig`` plus the ramp: ``ramp_steps`` daily rounds build a position, and the stop sits
    ``stop_vols`` entry vols from the average entry."""

    ramp_steps: int = 3
    stop_vols: Decimal = Decimal(8)

    def __post_init__(self) -> None:
        """The base checks, then the ramp's own (a bad step count or stop width raises ``ValueError``)."""
        super().__post_init__()
        self.ramp_params()

    def ramp_params(self) -> RampParams:
        """The ramp rule's parameters."""
        return RampParams(steps=self.ramp_steps, stop_vols=self.stop_vols)


def _side_word(side: int) -> str:
    return "long" if side > 0 else "short"


class TrendVolRamp(TrendVol):
    """``trend_vol`` with scale in entries and an hourly volatility stop (module docstring)."""

    def __init__(
        self,
        config: TrendVolRampConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Record,
        seeds: Mapping[str, Sequence[DailyClose]] | None = None,
        last_start_ns: int | None = None,
        stops: Stops | None = None,
    ) -> None:
        super().__init__(config, gate, blocks, record, seeds, last_start_ns)
        self._ramp = config.ramp_params()
        self.full_plan: PortfolioPlan | None = None
        # Each stopped coin: (stopped side, the plan day when the stop was sent).
        self.stopped: dict[str, tuple[int, date | None]] = {}
        self._stop_orders: dict[ClientOrderId, tuple[str, uuid.UUID]] = {}
        # The stop close being submitted: a denial can arrive inside ``submit_order``, before it is registered.
        self._sending_stop: tuple[str, uuid.UUID] | None = None
        for coin, (decided_ns, side) in (stops or {}).items():
            if coin in self._closes:
                self._restore_stop(coin, decided_ns, side)

    # Plan and rounds ------------------------------------------------------------------------

    def _make_plan(self, day: date) -> bool:
        # The sized plan becomes the full plan only when it was made; a failed plan keeps the old one.
        if not super()._make_plan(day):
            return False
        self.full_plan = self.plan
        return True

    def _rebalance(self, reason: str, skip: frozenset[str] = frozenset()) -> None:
        # Build this round's working plan from the full plan (never from ``self.plan``), then rebalance to it.
        full = self.full_plan
        if full is None:
            super()._rebalance(reason, skip)
            return
        may_grow = reason == DAILY
        if may_grow:
            self._clear_stops(full)
        coins: dict[str, CoinPlan] = {}
        held = set(skip)
        for coin in self._coins:
            key = str(coin)
            item = full.coins[key]
            if key in skip:
                coins[key] = item
                continue
            working = self._working_target(coin, item, reason, may_grow, held)
            weight = Decimal(0) if item.target_notional == 0 else item.weight * working / item.target_notional
            coins[key] = dataclasses.replace(item, weight=weight, target_notional=working)
        self.plan = dataclasses.replace(full, coins=coins)
        super()._rebalance(reason, frozenset(held))

    def _working_target(
        self, coin: InstrumentId, item: CoinPlan, reason: str, may_grow: bool, held: set[str]
    ) -> Decimal:
        key = str(coin)
        full = item.target_notional
        current_qty = self._qty(coin)
        mid = guard.mid(self.cache, coin)
        if key in self.stopped:
            working = Decimal(0)
            current = Decimal(0) if current_qty == 0 or mid is None else current_qty * mid
        else:
            if current_qty == 0:
                current = Decimal(0)
            elif mid is None:
                log.warning("ramp coin has no mid", extra={"fields": {"code": "ramp_no_mid", "instrument": key}})
                held.add(key)
                return full
            else:
                current = current_qty * mid
            working = ramp_target(full, current, self._ramp.steps, may_grow)
            if (
                not may_grow
                and full != 0
                and (current == 0 or ((current > 0) == (full > 0) and abs(current) < abs(full)))
            ):
                # A hold, never encoded as a target (AC-3): the base skips the coin entirely this round.
                log.info("ramp holds the coin", extra={"fields": {"code": "ramp_hold_grow", "instrument": key}})
                held.add(key)
            elif may_grow and mid is not None and working != 0:
                working = self._at_least_min_qty(coin, full, working, current_qty, mid)
        log.info(
            "ramp working target",
            extra={
                "fields": {
                    "code": "ramp_target",
                    "instrument": key,
                    "reason": reason,
                    "full": str(full),
                    "current": str(current),
                    "working": str(working),
                    "may_grow": may_grow,
                }
            },
        )
        return working

    def _at_least_min_qty(
        self, coin: InstrumentId, full: Decimal, working: Decimal, current_qty: Decimal, mid: Decimal
    ) -> Decimal:
        # A growing step (or a flip's open leg) smaller than the instrument's minimum targets the minimum on
        # that side, capped at the full target's quantity, so a small coin still builds.
        instrument = self._instrument(coin)
        if instrument.min_quantity is None:
            return working
        minimum: Decimal = instrument.min_quantity.as_decimal()
        increment: Decimal = instrument.size_increment.as_decimal()
        same_side = current_qty != 0 and (current_qty > 0) == (working > 0)
        base = abs(current_qty) if same_side else Decimal(0)
        step_qty = abs(target_quantity(working, mid, increment)) - base
        if step_qty <= 0 or step_qty >= minimum:
            return working
        bumped = min(abs(target_quantity(full, mid, increment)), base + minimum)
        if bumped <= base + step_qty:
            return working
        log.info(
            "ramp step raised to the minimum quantity",
            extra={
                "fields": {
                    "code": "ramp_step_min_qty",
                    "instrument": str(coin),
                    "step_qty": str(step_qty),
                    "min_quantity": str(minimum),
                }
            },
        )
        return (Decimal(1) if working > 0 else Decimal(-1)) * bumped * mid

    # Stop block -----------------------------------------------------------------------------

    def _step(self, coin: InstrumentId, *, first: bool) -> None:
        # A stopped coin is only ever reduced: flat, or with its stop close still working, nothing is sent.
        key = str(coin)
        if key in self.stopped and (self._qty(coin) == 0 or self._close_pending(coin)):
            self._drop_grow(key)
            return
        super()._step(coin, first=first)

    def _send_grow(self, key: str) -> None:
        # A grow queued or retried before the stop can never undo it (AC-5).
        if key in self.stopped:
            self._drop_grow(key)
            return
        super()._send_grow(key)

    def _drop_grow(self, key: str) -> None:
        if key in self._grows:
            self._grows.remove(key)
        self._cancel_timer(f"{_RETRY_PREFIX}{key}")

    def _clear_stops(self, full: PortfolioPlan) -> None:
        # Daily run only: a stop clears once a later plan's signal is not the stopped side (AC-5).
        for key, (side, stop_day) in list(self.stopped.items()):
            item = full.coins.get(key)
            if item is None or self.plan_day is None or (stop_day is not None and self.plan_day <= stop_day):
                continue
            signal = item.signal if item.eligible else None
            if stop_holds([signal], side):
                continue
            del self.stopped[key]
            self._cancel_timer(f"{_STOP_RETRY_PREFIX}{key}")
            log.info(
                "ramp stop cleared",
                extra={
                    "fields": {
                        "code": "ramp_stop_cleared",
                        "instrument": key,
                        "stopped_side": _side_word(side),
                        "signal": signal,
                        "plan_day": self.plan_day.isoformat(),
                    }
                },
            )

    # Hourly stop check ----------------------------------------------------------------------

    def on_start(self) -> None:
        """The base's start, then the hourly stop check armed at the next full hour."""
        super().on_start()
        self._arm_stop_check()

    def on_stop(self) -> None:
        """Cancel the stop check and stop retries, then the base's stop."""
        for name in list(self.clock.timer_names):
            if name == _STOP_CHECK or name.startswith(_STOP_RETRY_PREFIX):
                self.clock.cancel_timer(name)
        super().on_stop()

    def _arm_stop_check(self) -> None:
        at = (self.clock.timestamp_ns() // HOUR_NS + 1) * HOUR_NS
        self._alert(_STOP_CHECK, at, self._on_stop_check)

    def _on_stop_check(self, event: object) -> None:
        self._arm_stop_check()
        if not self._started:
            return
        for coin in self._coins:  # runs while paused or halted too: a stop only reduces
            try:
                self._check_stop(coin)
            except Exception:
                log.exception(
                    "ramp stop check failed",
                    extra={"fields": {"code": "ramp_stop_check_failed", "instrument": str(coin)}},
                )

    def _check_stop(self, coin: InstrumentId) -> None:
        key = str(coin)
        if key in self.stopped:
            return  # its close is sent; a refused one retries on its own timer
        positions = self.cache.positions_open(instrument_id=coin, strategy_id=self.id)
        if not positions:
            return
        if self.cache.orders_open(instrument_id=coin, strategy_id=self.id) or self.cache.orders_inflight(
            instrument_id=coin, strategy_id=self.id
        ):
            return
        position = positions[0]
        signed = self._qty(coin)
        if signed == 0:
            return
        side = 1 if signed > 0 else -1
        entry_px = Decimal(str(position.avg_px_open))
        entry_vol = self._entry_vol(key, position.ts_opened)
        if entry_vol is None:
            return
        quote = self.cache.quote_tick(coin)
        now = self.clock.timestamp_ns()
        if quote is None or Decimal(now - quote.ts_event) / NS_PER_S > self._cfg.stale_quote_s:
            log.warning(
                "ramp stop mark is stale", extra={"fields": {"code": "ramp_stop_mark_stale", "instrument": key}}
            )
            return
        mark = (quote.bid_price.as_decimal() + quote.ask_price.as_decimal()) / 2
        if not stop_hit(side, entry_px, mark, entry_vol, self._ramp.stop_vols):
            return
        decision = uuid.uuid7()
        inputs: JsonObject = {
            "side": side,
            "entry_px": entry_px,
            "entry_vol": entry_vol,
            "mark": mark,
            "stop_vols": self._ramp.stop_vols,
            "current_qty": signed,
            "plan_day": None if self.plan_day is None else self.plan_day.isoformat(),
        }
        self._record_decision(decision, key, Intent.EXIT, -signed, STOP_REASON, inputs, None)
        self.stopped[key] = (side, self.plan_day)
        self._drop_grow(key)
        self._send_stop_close(coin, decision)
        log.warning(
            "ramp stop hit",
            extra={
                "fields": {
                    "code": "ramp_stop_hit",
                    "instrument": key,
                    "side": _side_word(side),
                    "entry_px": str(entry_px),
                    "mark": str(mark),
                    "entry_vol": str(entry_vol),
                    "stop_vols": str(self._ramp.stop_vols),
                }
            },
        )

    def _entry_vol(self, key: str, ts_opened: int) -> Decimal | None:
        # The coin's vol over ``vol_days`` up to the day before the position opened; today's plan vol otherwise.
        open_day = datetime.fromtimestamp(ts_opened / NS_PER_S, tz=UTC).date()
        closes = [item.close for item in self.closes_until(open_day - timedelta(days=1))[key]]
        days = self._cfg.vol_days
        if len(closes) >= days + 1:
            return sample_vol(closes, days)
        full = self.full_plan
        fallback = None if full is None else full.coins[key].vol
        log.info(
            "ramp entry vol from today's plan",
            extra={
                "fields": {
                    "code": "ramp_entry_vol_fallback",
                    "instrument": key,
                    "open_day": open_day.isoformat(),
                    "closes": len(closes),
                    "vol": None if fallback is None else str(fallback),
                }
            },
        )
        return fallback

    def _send_stop_close(self, coin: InstrumentId, decision: uuid.UUID) -> None:
        # No ``track`` and no ``coin_exit``: the rebalance and the dollar exit re-entry never see this order.
        signed = self._qty(coin)
        if signed == 0:
            return
        self._sending_stop = (str(coin), decision)
        try:
            client_id = self._submit(coin, -signed, reduce_only=True, tags=[f"{DECISION_TAG}{decision}"])
            if self._sending_stop is not None:  # not refused while it was being sent
                self._stop_orders[client_id] = self._sending_stop
        finally:
            self._sending_stop = None

    def on_order_filled(self, event: OrderFilled) -> None:
        """The base's fill handling; a stop close that filled completely is forgotten."""
        super().on_order_filled(event)
        if event.client_order_id in self._stop_orders:
            order = self.cache.order(event.client_order_id)
            if order is None or order.is_closed:
                del self._stop_orders[event.client_order_id]

    def _order_refused(self, client_order_id: ClientOrderId, why: str) -> None:
        # A refused stop close is retried after ``retry_wait_s`` while its coin is still stopped (AC-4).
        tagged = self._stop_orders.pop(client_order_id, None)
        if tagged is None and self._sending_stop is not None:
            tagged, self._sending_stop = self._sending_stop, None
        if tagged is None:
            super()._order_refused(client_order_id, why)
            return
        key, decision = tagged
        log.warning(
            "ramp stop close refused",
            extra={"fields": {"code": "ramp_stop_close_refused", "instrument": key, "why": why}},
        )
        at = self.clock.timestamp_ns() + self._cfg.retry_wait_s * NS_PER_S
        self._alert(f"{_STOP_RETRY_PREFIX}{key}", at, functools.partial(self._on_stop_retry, key, decision))

    def _on_stop_retry(self, key: str, decision: uuid.UUID, event: object) -> None:
        coin = InstrumentId.from_str(key)
        try:
            if key in self.stopped and not self._close_pending(coin):
                self._send_stop_close(coin, decision)
        except Exception:
            log.exception(
                "ramp stop check failed", extra={"fields": {"code": "ramp_stop_check_failed", "instrument": key}}
            )

    # Restart --------------------------------------------------------------------------------

    def _run_ns(self, day: date) -> int:
        # When plan day ``day``'s daily run happens: day + 1 at ``run_at_utc``.
        hour, minute = self._cfg.run_at()
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) + timedelta(days=1)
        return int(at.timestamp()) * NS_PER_S

    def _restore_stop(self, key: str, decided_ns: int, side: int) -> None:
        # Rebuild the coin's daily signal for each plan day whose run came after the stop (AC-6).
        days = sorted(self._closes[key])
        stop_day = datetime.fromtimestamp(decided_ns / NS_PER_S, tz=UTC).date()
        # The plan day that was current when the stop was sent: the newest whose run is at or before it.
        stop_plan_day = stop_day - timedelta(days=1)
        if self._run_ns(stop_plan_day) > decided_ns:
            stop_plan_day -= timedelta(days=1)
        partial = not days or days[0] > stop_plan_day
        newest = days[-1] if days else stop_plan_day
        params = dataclasses.replace(self._params, min_history_days=self._params.lookback_days + 1)
        signals: list[int | None] = []
        day = stop_plan_day + timedelta(days=1)
        while day <= newest:
            signal: int | None = None
            if day in self._closes[key]:
                try:
                    item = portfolio_plan({key: self.closes_until(day)[key]}, Decimal(1), params).coins[key]
                    signal = item.signal if item.eligible else None
                except ValueError:  # PortfolioError is a ValueError
                    signal = None
            if signal is None:
                partial = True
            else:
                signals.append(signal)
            day += timedelta(days=1)
        fields: JsonObject = {
            "code": "ramp_stop_restored",
            "instrument": key,
            "stopped_side": _side_word(side),
            "decided_ns": decided_ns,
            "signals": list(signals),
            "partial": partial,
        }
        if not partial and not stop_holds(signals, side):
            log.info(
                "ramp stop dropped, the signal changed since", extra={"fields": {**fields, "code": "ramp_stop_dropped"}}
            )
            return
        self.stopped[key] = (side, max(newest, stop_plan_day))
        log.info("ramp stop restored", extra={"fields": fields})

    # Snapshot -------------------------------------------------------------------------------

    def plan_snapshot(self) -> Mapping[str, object]:
        """``TrendVol``'s ``portfolio_plan`` data with full targets, plus ``held_pct``, ``stopped_side``,
        ``ramp_steps``, and ``stop_vols`` (AC-7)."""
        data = dict(super().plan_snapshot())
        full = self.full_plan
        raw = data.get("coins")
        coins: list[JsonObject] = []
        for entry in raw if isinstance(raw, list) else []:
            coin_data: dict[str, JsonValue] = dict(entry) if isinstance(entry, Mapping) else {}
            key = str(coin_data.get("instrument_id"))
            stopped = self.stopped.get(key)
            coin_data["stopped_side"] = None if stopped is None else _side_word(stopped[0])
            coin_data["held_pct"] = None
            item = None if full is None else full.coins.get(key)
            if item is not None:
                coin = InstrumentId.from_str(key)
                mid = guard.mid(self.cache, coin)
                instrument = self.cache.instrument(coin)
                coin_data["target_notional"] = str(item.target_notional)
                coin_data["weight"] = str(item.weight)
                if mid is not None and mid > 0 and instrument is not None:
                    increment = instrument.size_increment.as_decimal()
                    coin_data["target_qty"] = str(target_quantity(item.target_notional, mid, increment))
                    if item.target_notional != 0:
                        held = self._qty(coin) * mid / item.target_notional * 100
                        coin_data["held_pct"] = str(held.quantize(_CENT))
            coins.append(coin_data)
        data["coins"] = coins
        data["ramp_steps"] = self._ramp.steps
        data["stop_vols"] = str(self._ramp.stop_vols)
        return data
