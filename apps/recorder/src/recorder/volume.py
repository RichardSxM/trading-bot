"""The extra disk check (spec 0005 AC-18): on the VPS the catalog must be the mounted disk."""

from pathlib import Path

MARKER = ".kfs-volume"


class CatalogVolumeMissing(Exception):
    """The catalog folder has no marker, so the extra disk is not mounted there."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} has no {MARKER}: the catalog disk is not mounted")
        self.path = path


def check_catalog_volume(catalog_path: Path, *, required: bool) -> None:
    """Raise ``CatalogVolumeMissing`` when ``required`` and the marker is not in the catalog.

    bootstrap.sh writes the marker on the extra disk only, so a missing mount leaves the bind
    source an empty folder on the system disk, and the recorder must not fill it.
    """
    if required and not (catalog_path / MARKER).is_file():
        raise CatalogVolumeMissing(catalog_path)
