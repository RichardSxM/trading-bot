"""``lab import-funding``: Kraken's funding export into the candles tree (spec 0014 AC-2a).

Reads only ``exports/PF_XBTUSD.csv`` from the ZIP (or a bare CSV with the same columns). Every row is
checked before the first write; then rows are merged by charge time with the fetch's rule, so a
second import adds nothing. A 4 h era rate is stored as is: gate G-4 found it is charged once per
4 h period, so no times 4. No network request, no database.
"""

import csv
import io
import logging
import sys
import zipfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from lab.candle_fetch import EXIT_FAILED, EXIT_OK, SYMBOL, candle_tree, log_bad_rows, merge_into_tree
from lab.settings import LabSettings
from trading_core.lab.candles import FundingRow, check_funding_stamp
from trading_core.lab.errors import BadRowsError
from trading_core.marketdata.hours import SECOND_NS

log = logging.getLogger(__name__)

MEMBER: Final = f"exports/{SYMBOL}.csv"
HEADER: Final = ["timestamp", "tradeable", "absolute_rate", "relative_rate"]
EXIT_USAGE: Final = 64


class ExportFileError(ValueError):
    """The file is missing, has no ``exports/PF_XBTUSD.csv``, a wrong header, or another tradeable (exit 64)."""


def read_export(path: Path) -> list[dict[str, str]]:
    """The export's rows. Raises ``ExportFileError`` on a missing file, member, header, or tradeable."""
    if not path.is_file():
        raise ExportFileError(f"{path} does not exist")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            try:
                text = archive.read(MEMBER).decode("utf-8")
            except KeyError:
                raise ExportFileError(f"{path} has no member {MEMBER}") from None
    else:
        text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != HEADER:
        raise ExportFileError(f"{path}: the header is {reader.fieldnames}, expected {HEADER}")
    rows = list(reader)
    others = sorted({r["tradeable"] for r in rows} - {SYMBOL})
    if others:
        raise ExportFileError(f"{path}: tradeables other than {SYMBOL}: {', '.join(others[:5])}")
    return rows


def export_funding_rows(rows: list[dict[str, str]]) -> list[FundingRow]:
    """Each export row as a funding row, every one checked. Raises ``BadRowsError`` listing all bad rows."""
    bad: list[tuple[str, str, str]] = []
    parsed: list[FundingRow] = []
    for row in rows:
        try:
            stamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            bad.append((row["timestamp"], "timestamp", row["timestamp"]))
            continue
        try:
            rate = Decimal(row["relative_rate"])
        except InvalidOperation:
            bad.append((row["timestamp"], "relative_rate", row["relative_rate"]))
            continue
        stamp_ns = int(stamp.timestamp()) * SECOND_NS
        problems = check_funding_stamp(stamp_ns, rate)
        bad.extend((row["timestamp"], name, value) for _, name, value in problems)
        if not problems:
            parsed.append(FundingRow.from_stamp(stamp_ns, rate, "export"))
    if bad:
        raise BadRowsError("funding export", bad)
    return parsed


def import_funding(settings: LabSettings, path: Path) -> int:
    """Merge the export into the candles tree. Returns the exit code."""
    try:
        rows = export_funding_rows(read_export(path))
        added, present, touched = merge_into_tree(candle_tree(settings.lab_catalog_dir), rows, "funding export")
    except ExportFileError as exc:
        print(f"lab import-funding: {exc}", file=sys.stderr)
        log.error("lab_bad_input", extra={"fields": {"problems": [str(exc)]}})
        return EXIT_USAGE
    except BadRowsError as exc:
        log_bad_rows(exc)
        return EXIT_FAILED
    log.info(
        "lab_funding_imported",
        extra={"fields": {"rows_added": added, "rows_present": present, "months_touched": touched}},
    )
    return EXIT_OK
