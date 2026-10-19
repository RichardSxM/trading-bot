"""The gate's settings (spec 0015, *Configuration required*)."""

from pathlib import Path

from trading_core.settings import Settings


class GateSettings(Settings):
    """Where ``gate import`` reads bundle files (mounted from ``/var/lib/kfs/bundles`` on the VPS)."""

    gate_bundle_dir: Path = Path("/bundles")
