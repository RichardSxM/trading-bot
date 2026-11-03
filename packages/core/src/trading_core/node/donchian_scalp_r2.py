"""``DonchianScalpR2``: r1's signals traded with maker orders and a volatility floor (spec 0026).

A demo only paper strategy with no gate pass: it trades only after a manual ``candidate → paper`` promote.
Its own module so its code hash is its own. It runs ``DonchianMidlineRule`` (imported unchanged) on the
maker base ``MakerBarRuleStrategy``; the frozen r1 module is not imported, so the seed join and the spread
check are written again here.

The warmup seed (spec 0025 AC-7, AC-10, AC-14 here): candles fetched before the node runs are held until
the first live bar, which is partial and so is ignored. If that bar closes more than two intervals after
the seed's last close the seed is dropped as stale; otherwise it is fed through the base's ATR and the
rule, and any live bar at or before the seed's last close is ignored too. The seed never records a
decision or trades.

Two entry filters, once the base's own refusals are clear and in this order (``_pre_entry``): the
volatility floor (AC-13), which skips an entry whose ATR is below ``atr_min_bps`` of the close as
``vol_low``, then r1's spread check, which skips one whose quote spread is wider than ``spread_max_atr``
ATR as ``spread_wide``. Neither sends an order or raises a risk event.

The take profit (``take_profit_pct``, off when ``None``): at a bar close in a position, once the time stop
has not fired, a close at or beyond ``take_profit_pct`` percent of the entry price in the trade's favour
exits as ``take_profit``, before the rule's own exit. Like the other planned exits it rests a maker limit
at the touch (the base's ``_decide_exit``); the stop keeps resting until the position is flat.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import Bar
from nautilus_trader.model.position import Position

from trading_core.node.bar_rule_maker import MakerBarRuleConfig, MakerBarRuleStrategy, order_side
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian_midline import DonchianMidlineRule

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "donchian_scalp_r2"
BAR_SPEC: Final = "5-MINUTE-LAST"
VOL_LOW: Final = "vol_low"
SPREAD_WIDE: Final = "spread_wide"
SEED_FAILED: Final = "warmup_seed_failed"
SEED_STALE: Final = "seed_stale"
TAKE_PROFIT: Final = "take_profit"
BPS: Final = Decimal(10_000)
PCT: Final = Decimal(100)
# More than this many intervals between the seed's last close and the first live bar's means more than
# one bar is missing between them.
SEED_JOIN_INTERVALS: Final = 2


class DonchianScalpR2Config(MakerBarRuleConfig, frozen=True, kw_only=True):
    """``donchian_scalp_r2``'s settings on top of the maker base's (AC-14)."""

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 60
    entry_bars: int = 20
    stop_atr: Decimal = Decimal(3)
    ema_period: int = 200
    atr_min_bps: Decimal | None = Decimal(10)
    spread_max_atr: Decimal | None = None
    take_profit_pct: Decimal | None = None

    def __post_init__(self) -> None:
        """The base's checks, then this strategy's."""
        super().__post_init__()
        if self.bar_spec != BAR_SPEC:
            raise ValueError(f"bar_spec must be {BAR_SPEC}, got {self.bar_spec!r}")
        if self.entry_bars < 2:
            raise ValueError(f"entry_bars must be at least 2, got {self.entry_bars}")
        if self.stop_atr < 1:
            raise ValueError(f"stop_atr must be at least 1, got {self.stop_atr}")
        if self.ema_period < 2:
            raise ValueError(f"ema_period must be at least 2, got {self.ema_period}")
        if self.spread_max_atr is not None and self.spread_max_atr <= 0:
            raise ValueError(f"spread_max_atr must be above 0, got {self.spread_max_atr}")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            raise ValueError(f"take_profit_pct must be above 0, got {self.take_profit_pct}")


def take_profit_px(side: Side, entry_px: Decimal, pct: Decimal) -> Decimal:
    """The price ``pct`` percent of ``entry_px`` in the trade's favour: above it for a long, below for a short."""
    move = entry_px * pct / PCT
    return entry_px + move if side == Side.LONG else entry_px - move


def take_profit_hit(side: Side, entry_px: Decimal, pct: Decimal, close: Decimal) -> bool:
    """True when ``close`` is at or beyond the take profit price (``take_profit_px``)."""
    target = take_profit_px(side, entry_px, pct)
    return close >= target if side == Side.LONG else close <= target


class DonchianScalpR2(MakerBarRuleStrategy):
    """``MakerBarRuleStrategy`` with ``DonchianMidlineRule`` on 5 minute bars, a seed, and two entry filters."""

    def __init__(
        self,
        config: DonchianScalpR2Config,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Callable[[WriteItem], None],
        seed: Sequence[BarInput] = (),
    ) -> None:
        rule = DonchianMidlineRule(entry_bars=config.entry_bars, stop_atr=config.stop_atr, ema_period=config.ema_period)
        super().__init__(config, gate, blocks, record, rule)
        self._r2 = config
        self._seed: tuple[BarInput, ...] = tuple(seed)
        self._seed_pending = bool(self._seed)
        self._seed_last_ns: int | None = self._seed[-1].ts_ns if self._seed else None

    # Warmup seed ----------------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Join the seed at the first live bar, ignore bars it already covers, else the base's step."""
        if bar.bar_type != self.bar_type:
            return
        if self._seed_pending:
            self._seed_pending = False
            self._join_seed(bar.ts_event)
            return  # the first live bar covers only part of its interval
        if self._seed_last_ns is not None and bar.ts_event <= self._seed_last_ns:
            return
        super().on_bar(bar)

    def _join_seed(self, first_close_ns: int) -> None:
        assert self._seed_last_ns is not None  # a pending seed is not empty
        gap = first_close_ns - self._seed_last_ns
        seed, self._seed = self._seed, ()
        if gap > SEED_JOIN_INTERVALS * self._r2.bar_interval_ns:
            self._seed_last_ns = None
            log.warning(
                "warmup seed dropped, too old for the first live bar",
                extra={"fields": {"code": SEED_FAILED, "reason": SEED_STALE, "gap_s": gap // 1_000_000_000}},
            )
            return
        try:
            for closed in seed:
                atr = self._atr.update(closed.high, closed.low, closed.close)
                self.rule.update(closed, atr)
            if self.rule.entry() is not None:
                self.rule.consumed()  # the seed never trades
        except Exception:
            # Fail safe: warm up from live bars on whatever was fed; nothing was decided.
            log.exception("warmup seed feed failed", extra={"fields": {"code": SEED_FAILED, "reason": "seed_feed"}})
            return
        log.info(
            "warmup seed fed",
            extra={"fields": {"code": "warmup_seed_fed", "bars": len(seed), "ready": self.rule.ready()}},
        )

    # Take profit -----------------------------------------------------------------------------

    def _exit_step(self, bar: BarInput, atr: Decimal, position: Position) -> bool:
        """The time stop first (the base's), then the take profit, then the rule's own exit."""
        pct = self._r2.take_profit_pct
        if pct is None or self.bars_held >= self._r2.time_stop_bars:
            return super()._exit_step(bar, atr, position)
        entry_px = self._entry_px if self._entry_px is not None else Decimal(str(position.avg_px_open))
        side = Side.LONG if position.is_long else Side.SHORT
        if not take_profit_hit(side, entry_px, pct, bar.close):
            return super()._exit_step(bar, atr, position)
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "bars_held": self.bars_held,
            "stop": self._stop_px,
            "entry_px": entry_px,
            "take_profit_pct": pct,
            "take_profit_px": take_profit_px(side, entry_px, pct),
        }
        self._decide_exit(position, TAKE_PROFIT, inputs, maker=True)
        return True

    # Entry filters --------------------------------------------------------------------------

    def _pre_entry(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> bool:
        """The volatility floor, then the spread check; False after recording the refusal."""
        return self._vol_ok(bar, atr, signal) and self._spread_ok(bar, atr, signal)

    def _vol_ok(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> bool:
        floor = self._r2.atr_min_bps
        if floor is None:
            return True
        atr_bps = atr / bar.close * BPS
        if atr_bps >= floor:
            return True
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "structural_stop": signal.structural_stop,
            "atr_bps": atr_bps,
            "atr_min_bps": floor,
        }
        now = self.clock.timestamp_ns()
        self._record_decision(
            uuid.uuid7(), Intent.ENTER, order_side(signal.side), signal.reason, inputs, False, VOL_LOW, now
        )
        log.info(
            "entry skipped, volatility low",
            extra={"fields": {"code": "entry_skipped_vol", "atr_bps": str(atr_bps), "floor": str(floor)}},
        )
        return False

    def _spread_ok(self, bar: BarInput, atr: Decimal, signal: EnterSignal) -> bool:
        limit_atr = self._r2.spread_max_atr
        quote = self.cache.quote_tick(self._instrument_id)
        if limit_atr is None or quote is None:
            return True  # the base logs a missing quote
        bid, ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        spread, limit = ask - bid, limit_atr * atr
        if spread <= limit:
            return True
        now = self.clock.timestamp_ns()
        inputs: JsonObject = {
            **self._bar_inputs(bar, atr),
            "structural_stop": signal.structural_stop,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_limit": limit,
            "mid": (bid + ask) / 2,
            "quote_age_ms": (now - quote.ts_event) // 1_000_000,
        }
        self._record_decision(
            uuid.uuid7(), Intent.ENTER, order_side(signal.side), signal.reason, inputs, False, SPREAD_WIDE, now
        )
        log.info(
            "entry skipped, spread wide",
            extra={"fields": {"code": "entry_skipped_spread", "spread": str(spread), "limit": str(limit)}},
        )
        return False
