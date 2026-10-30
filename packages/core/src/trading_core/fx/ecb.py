"""The ECB Data Portal daily USD per EUR reference rate (spec 0024 AC-15), pure: no I/O.

``ecb_url`` builds the request, ``parse_ecb_csv`` turns a ``format=csvdata`` body into
``FxRateRow`` values. The parser reads ``TIME_PERIOD`` and ``OBS_VALUE`` by header name and
rejects the whole body on any bad line, so a format change fails closed and writes nothing.
"""

import csv
import io
import re
from datetime import date
from decimal import Decimal
from typing import Final
from urllib.parse import urlencode

from trading_core.recording.rows import FxRateRow

DATE_COLUMN: Final = "TIME_PERIOD"
RATE_COLUMN: Final = "OBS_VALUE"
# A plain positive decimal, never an exponent, NaN, or sign: ``Decimal`` alone would accept those.
_RATE: Final = re.compile(r"\d+(\.\d+)?")


class FxParseError(ValueError):
    """An ECB response that cannot be trusted as a whole; ``reason`` says why, for the log."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def ecb_url(base: str, start: date, end: date) -> str:
    """The Data Portal request for the daily observations from ``start`` to ``end``, both inclusive."""
    query = urlencode({"startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"})
    return f"{base}?{query}"


def _rate(text: str, line: int) -> Decimal:
    if _RATE.fullmatch(text) is None:
        raise FxParseError(f"line {line}: rate {text!r} is not a plain decimal")
    rate = Decimal(text)
    if rate <= 0:
        raise FxParseError(f"line {line}: rate {text!r} is not positive")
    return rate


def _date(text: str, line: int, start: date, end: date) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise FxParseError(f"line {line}: date {text!r} is not YYYY-MM-DD") from None
    if len(text) != len("YYYY-MM-DD"):
        raise FxParseError(f"line {line}: date {text!r} is not YYYY-MM-DD")
    if not start <= day <= end:
        raise FxParseError(f"line {line}: date {day} is outside the request {start} to {end}")
    return day


def parse_ecb_csv(text: str, start: date, end: date) -> list[FxRateRow]:
    """The EUR/USD rows of a ``csvdata`` body requested for ``start`` to ``end``, in date order.

    Raises ``FxParseError`` for a body that is not CSV, lacks either column, holds a non
    numeric or non positive rate, repeats a date, or holds a date outside the request.
    """
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        header = reader.fieldnames
        if header is None or DATE_COLUMN not in header or RATE_COLUMN not in header:
            raise FxParseError(f"missing column {DATE_COLUMN} or {RATE_COLUMN} in header {header}")
        rates: dict[date, Decimal] = {}
        for record in reader:
            line = reader.line_num
            if None in record or any(value is None for value in record.values()):
                raise FxParseError(f"line {line}: field count differs from the header")
            day = _date(record[DATE_COLUMN].strip(), line, start, end)
            if day in rates:
                raise FxParseError(f"line {line}: date {day} appears twice")
            rates[day] = _rate(record[RATE_COLUMN].strip(), line)
    except csv.Error as error:
        raise FxParseError(f"bad CSV: {error}") from None
    return [FxRateRow(rate_date=day, rate=rates[day]) for day in sorted(rates)]
