"""Where the files of a catalog tree live (spec 0007, *Catalog layout*).

``data/<table>/<instrument>/<interval>.parquet`` (the names ``ParquetDataCatalog`` reads),
``gaps/<day>.parquet``, and ``manifests/<day>.json``, the day's commit point.
"""

from pathlib import Path
from typing import Final

from nautilus_trader.model.data import FundingRateUpdate, OrderBookDelta, TradeTick
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.catalog.parquet import _parse_filename_timestamps, _timestamps_to_filename
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

from trading_core.marketdata.hours import hour_last_ns

DELTAS_TABLE: Final[str] = class_to_filename(OrderBookDelta)
TRADES_TABLE: Final[str] = class_to_filename(TradeTick)
INSTRUMENTS_TABLE: Final[str] = class_to_filename(CryptoPerpetual)
FUNDING_TABLE: Final[str] = class_to_filename(FundingRateUpdate)


def hour_filename(hour: int) -> str:
    """The file name of one whole hour, ``<first ns>_<last ns>`` as Nautilus formats it."""
    name: str = _timestamps_to_filename(hour, hour_last_ns(hour))
    return name


def parse_hour_filename(name: str) -> int | None:
    """The hour a file name holds, or ``None`` when it is not exactly one whole hour."""
    interval = _parse_filename_timestamps(name)
    if interval is None or interval[1] != hour_last_ns(interval[0]):
        return None
    hour: int = interval[0]
    return hour


class CatalogLayout:
    """The paths of one catalog tree under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def data_file(self, table: str, instrument_id: str, hour: int) -> Path:
        """``data/<table>/<instrument>/<interval>.parquet``, the name ``ParquetDataCatalog`` reads."""
        return self.root / "data" / table / urisafe_identifier(instrument_id) / hour_filename(hour)

    def gap_file(self, day: str) -> Path:
        """``gaps/<YYYY-MM-DD>.parquet``, the gaps that start on that UTC day."""
        return self.root / "gaps" / f"{day}.parquet"

    def manifest_file(self, day: str) -> Path:
        """``manifests/<YYYY-MM-DD>.json``, the closed day's size and gap totals."""
        return self.root / "manifests" / f"{day}.json"
