"""``DonchianScalpTp``: ``donchian_scalp``'s rule on 5 minute bars with live dollar exits and a warmup seed (demo only).

It is its own module with its own config, so the frozen ``donchian_scalp`` hash does not move. The rule, the
resting ATR stop, and the time stop are ``donchian_scalp``'s.

The dollar exits: while the position is protected, every quote is checked, and a close that would make
``take_profit_usd`` or lose ``stop_loss_usd`` (before fees) exits at market as ``take_profit`` or ``stop_loss``.
The ATR stop is the backstop if quotes stop.

The warmup seed: candles fetched before the node runs are held until the first live bar, which is partial
and so is ignored. If that bar closes more than two intervals after the seed's last close the seed is
dropped as stale; otherwise it is fed through the base's ATR and the rule, and any live bar at or before
the seed's last close is ignored too. The seed never trades. The join is written again here, as in r2,
six vote, and ``connors_rsi_tp``, so this module's frozen hash does not depend on theirs.
"""

import logging
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Final

from nautilus_trader.model.data import Bar, QuoteTick

from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy, Phase
from trading_core.node.strategy import PauseGate
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.donchian import DonchianRule
from trading_core.strategy.take_profit import dollar_exit

log = logging.getLogger(__name__)

STRATEGY_NAME: Final = "donchian_scalp_tp"
BAR_SPEC: Final = "5-MINUTE-LAST"
SEED_FAILED: Final = "warmup_seed_failed"
SEED_STALE: Final = "seed_stale"
# More than this many intervals between the seed's last close and the first live bar's means a hole.
SEED_JOIN_INTERVALS: Final = 2


class DonchianScalpTpConfig(BarRuleConfig, frozen=True, kw_only=True):
    """``donchian_scalp``'s rule settings plus the two dollar exits, per position and before fees."""

    bar_spec: str = BAR_SPEC
    time_stop_bars: int = 60
    entry_bars: int = 20
    stop_atr: Decimal = Decimal(3)
    take_profit_usd: Decimal = Decimal(3)
    stop_loss_usd: Decimal = Decimal(2)

    def __post_init__(self) -> None:
        """The base's checks, then the rule's and the dollar exits'."""
        super().__post_init__()
        if self.entry_bars < 2:
            raise ValueError(f"entry_bars must be at least 2, got {self.entry_bars}")
        if self.stop_atr < 1:
            raise ValueError(f"stop_atr must be at least 1, got {self.stop_atr}")
        if self.take_profit_usd <= 0:
            raise ValueError(f"take_profit_usd must be above 0, got {self.take_profit_usd}")
        if self.stop_loss_usd <= 0:
            raise ValueError(f"stop_loss_usd must be above 0, got {self.stop_loss_usd}")


class DonchianScalpTp(BarRuleStrategy):
    """``BarRuleStrategy`` with ``DonchianRule``, the dollar exits, and the warmup seed (see the module docstring)."""

    def __init__(
        self,
        config: DonchianScalpTpConfig,
        gate: PauseGate,
        blocks: EntryBlocks,
        record: Callable[[WriteItem], None],
        seed: Sequence[BarInput] = (),
    ) -> None:
        super().__init__(
            config, gate, blocks, record, DonchianRule(entry_bars=config.entry_bars, stop_atr=config.stop_atr)
        )
        self._tp = config
        self._seed: tuple[BarInput, ...] = tuple(seed)
        self._seed_pending = bool(self._seed)
        self._seed_last_ns: int | None = self._seed[-1].ts_ns if self._seed else None

    # Dollar exits ---------------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Exit at market once a protected position's close would reach either dollar exit."""
        if self.phase != Phase.PROTECTED or self._entry_px is None or self._side is None:
            return
        if tick.instrument_id != self._instrument_id:
            return
        try:
            bid, ask = tick.bid_price.as_decimal(), tick.ask_price.as_decimal()
            position = self._position()
            if position is None:
                return
            cfg = self._tp
            quantity = position.quantity.as_decimal()
            reason = dollar_exit(self._side, self._entry_px, quantity, bid, ask, cfg.take_profit_usd, cfg.stop_loss_usd)
            if reason is None:
                return
            inputs: JsonObject = {
                "bid": bid,
                "ask": ask,
                "entry_px": self._entry_px,
                "take_profit_usd": cfg.take_profit_usd,
                "stop_loss_usd": cfg.stop_loss_usd,
                "bars_held": self.bars_held,
                "stop": self._stop_px,
            }
            self._decide_exit(position, reason, inputs)
        except Exception:
            # Fail closed: no exit from this quote; the position keeps its stop and the next quote checks again.
            log.exception("dollar exit check failed", extra={"fields": {"code": "dollar_exit_failed"}})

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
        if gap > SEED_JOIN_INTERVALS * self._tp.bar_interval_ns:
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
