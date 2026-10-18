"""Test only strategies, registered only with ``LAB_TEST_STRATEGIES=1``.

``raise_probe`` is a ``maker_probe`` that raises at a set time, for the failure tests (spec 0008
AC-13). It raises out of ``on_quote_tick`` once a quote at or after ``raise_at_ns`` arrives, and
only in the entry mode ``raise_in_mode``, so with ``--pair-entry`` one variant fails and the other
completes.

``peek_probe`` is a ``macd_baseline`` that looks ahead, for the look ahead check (spec 0014 AC-19):
at each bar close it records a refused ``enter`` decision stamped at the previous bar's close, on
the side this bar moved to. So every decision about a bar is made from the next one, which the
full run has and a run cut between them does not.
"""

import uuid

from nautilus_trader.model.data import Bar, QuoteTick

from trading_core.node.macd_baseline import MacdBaseline, MacdBaselineConfig
from trading_core.node.maker_probe import MakerProbe, MakerProbeConfig
from trading_core.node.placeholder import Record
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, OrderSide
from trading_core.strategy.blocks import EntryBlocks


class ProbeError(Exception):
    """The deliberate failure ``raise_probe`` raises."""


class RaiseProbeConfig(MakerProbeConfig, frozen=True):
    """``maker_probe``'s settings plus when, and in which entry mode, to raise."""

    raise_at_ns: int = 0
    raise_in_mode: str = "maker"


class RaiseProbe(MakerProbe):
    """Trades like ``maker_probe`` until the first quote at or after ``raise_at_ns``, then raises."""

    def __init__(self, config: RaiseProbeConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self._raise_cfg = config

    def on_quote_tick(self, tick: QuoteTick) -> None:
        cfg = self._raise_cfg
        if cfg.entry_mode == cfg.raise_in_mode and tick.ts_event >= cfg.raise_at_ns:
            raise ProbeError(f"raise_probe: raised at {tick.ts_event} as configured")
        super().on_quote_tick(tick)


PEEK_REASON = "peek"


class PeekProbe(MacdBaseline):
    """Records a decision about each bar only once the next bar has closed, stamped at the earlier close."""

    def __init__(self, config: MacdBaselineConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> None:
        super().__init__(config, gate, blocks, record)
        self._previous: Bar | None = None

    def on_bar(self, bar: Bar) -> None:
        """Decide about the previous bar from this one; never trade."""
        if bar.bar_type != self.bar_type:
            return
        previous, self._previous = self._previous, bar
        if previous is None or bar.close == previous.close:
            return
        side = OrderSide.BUY if bar.close > previous.close else OrderSide.SELL
        self._record_decision(uuid.uuid4(), Intent.ENTER, side, PEEK_REASON, {}, False, PEEK_REASON, previous.ts_event)
