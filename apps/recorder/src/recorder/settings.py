"""The recorder's settings, apart from ``main.py`` so the health check loads them without Nautilus."""

from pydantic import Field

from trading_core.settings import CatalogDiskSettings, ContextSettings


class RecorderSettings(CatalogDiskSettings, ContextSettings):
    """The recorder's instruments, catalog folder, and queue watch level, plus the disk floors and the
    market context feed (spec 0009)."""

    # Pinned from the adapter's instrument provider (Kraken's live public data).
    recorder_instruments: str = "PF_XBTUSD.KRAKEN,PF_ETHUSD.KRAKEN"
    catalog_path: str = "/data/catalog"
    # True on the VPS (compose.vps.yaml): refuse to start unless the extra disk is mounted.
    catalog_require_marker: bool = False
    # The data queue depth, in percent of its capacity, that logs `recorder_queue_high` (AC-13).
    recorder_queue_warn_pct: int = Field(default=50, ge=1, le=99)
    # How long a missing message number may be overtaken before it counts as lost (AC-15): the
    # compactor's reorder window, so a row it would still put back in place is never called lost.
    recorder_sequence_confirm_s: int = Field(default=600, ge=1)

    @property
    def instrument_list(self) -> list[str]:
        return [i.strip() for i in self.recorder_instruments.split(",") if i.strip()]
