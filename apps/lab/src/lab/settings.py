"""The lab's settings (spec 0008, *Configuration required*). Simulation settings come from the run file, not here."""

from decimal import Decimal
from pathlib import Path

from pydantic import Field

from trading_core.settings import Settings


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
