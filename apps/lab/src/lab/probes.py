"""``raise_probe``: a ``maker_probe`` that raises at a set time, for the failure tests (spec 0008 AC-13).

Registered only with ``LAB_TEST_STRATEGIES=1``. It raises out of ``on_quote_tick`` once a quote
at or after ``raise_at_ns`` arrives, and only in the entry mode ``raise_in_mode``, so with
``--pair-entry`` one variant fails and the other completes.
"""

from nautilus_trader.model.data import QuoteTick

from trading_core.node.maker_probe import MakerProbe, MakerProbeConfig
from trading_core.node.placeholder import Record
from trading_core.node.strategy import PauseGate
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
