"""``StaleQuoteBlock``: the stale quote entry block every strategy shares (spec 0006 AC-14, spec 0014 AC-13).

Every ``stale_check_s`` seconds it compares the newest quote's age with ``stale_quote_s``. Past it,
the ``STALE_FEED`` entry block goes on and one ``stale_feed`` risk event is recorded; once quotes
are fresh again the block clears. Before the first quote the age counts from the start, so a fresh
node is not a stale feed. The paper loop checks every 1 s against 10 s; a candle run checks every
60 s against its run file's ``stale_quote_s`` (path points are about 20 s apart).
"""

import logging
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from nautilus_trader.model.identifiers import InstrumentId

from trading_core.recording.enums import RiskEventKind, Severity
from trading_core.recording.rows import JsonObject
from trading_core.strategy.blocks import BlockReason, EntryBlocks

log = logging.getLogger(__name__)

NS_PER_S: Final = 1_000_000_000


class StaleQuoteBlock:
    """Mixin for a ``KfsStrategy`` subclass that has ``_risk_event`` (as ``PlaceholderTimed`` does).

    Call ``start_stale_check`` from ``on_start``.
    """

    # Provided by the Nautilus strategy this is mixed into.
    clock: Any
    cache: Any

    def start_stale_check(
        self,
        *,
        timer: str,
        instrument_id: InstrumentId,
        run_id: UUID,
        blocks: EntryBlocks,
        stale_quote_s: Decimal,
        stale_check_s: int,
    ) -> None:
        """Arm the repeating check, named ``timer``, every ``stale_check_s`` seconds."""
        self._stale_instrument_id = instrument_id
        self._stale_run_id = run_id
        self._stale_blocks = blocks
        self._stale_quote_s = stale_quote_s
        self._stale_started_ns: int = self.clock.timestamp_ns()
        self._stale_since_ns: int | None = None
        self.clock.set_timer_ns(timer, stale_check_s * NS_PER_S, 0, 0, self._on_stale_check)

    def _on_stale_check(self, event: object) -> None:
        now = self.clock.timestamp_ns()
        quote = self.cache.quote_tick(self._stale_instrument_id)
        # Before the first quote, the feed counts from the start: a fresh node is not a stale feed.
        last_ns = self._stale_started_ns if quote is None else quote.ts_event
        stale = now - last_ns > int(self._stale_quote_s * NS_PER_S)
        if stale and self._stale_blocks.add(BlockReason.STALE_FEED):
            self._stale_since_ns = now
            self._risk_event(
                f"stale_feed:{self._stale_run_id}:{now}",
                RiskEventKind.STALE_FEED,
                Severity.WARNING,
                f"no quote for {self._stale_instrument_id} in {self._stale_quote_s} s; entries blocked",
                None,
                {"last_quote_ns": None if quote is None else quote.ts_event},
            )
        elif not stale and self._stale_blocks.clear(BlockReason.STALE_FEED):
            log.info("quotes fresh again", extra={"fields": {"code": "stale_feed_cleared"}})
            self._stale_since_ns = None

    def _risk_event(
        self,
        key: str,
        kind: RiskEventKind,
        severity: Severity,
        message: str,
        decision: UUID | None,
        details: JsonObject,
    ) -> None:
        """Record a risk event; the strategy class provides it."""
        raise NotImplementedError
