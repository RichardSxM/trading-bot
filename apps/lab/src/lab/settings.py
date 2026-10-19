"""The lab's settings (spec 0008, *Configuration required*). Simulation settings come from the run file, not here."""

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_core.settings import Settings


class ProbeSwitch(BaseSettings):
    """``LAB_TEST_STRATEGIES``: tests only, adds ``raise_probe`` to the strategy registry (spec 0008 AC-13).

    Read on its own because a run file is validated (exit 64) before the process boots.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    lab_test_strategies: bool = False


class LabSettings(Settings):
    """Where the catalog and reports live, the disk cap, and the worker count."""

    # Root of both source trees (``tardis/`` and ``own/``).
    lab_catalog_dir: Path = Path("/catalog-lab")
    # Reports, bundle files, and the paper CSV.
    lab_reports_dir: Path = Path("/lab-reports")
    # The cap for everything under lab_catalog_dir (AC-2).
    lab_catalog_cap_gb: Decimal = Field(default=Decimal(40), gt=0)
    # Worker processes for replay units: the laptop has 2 physical cores and a 3g container (AC-16).
    lab_workers: int = Field(default=2, ge=1, le=4)
    # The expected size of one converted Tardis day, for the cap check (AC-2).
    lab_tardis_day_gb: Decimal = Field(default=Decimal(3), gt=0)
    # Tests only: the registry also holds raise_probe (AC-13).
    lab_test_strategies: bool = False
    # The public Kraken Futures API the candle lab fetches from (spec 0014 AC-5, AC-22).
    lab_kraken_futures_url: str = "https://futures.kraken.com"
    # The pause between two Kraken requests (spec 0014 AC-5).
    lab_kraken_pause_ms: int = Field(default=250, ge=0, le=5_000)

    @field_validator("lab_kraken_futures_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("must be an https base URL")
        return value.rstrip("/")
