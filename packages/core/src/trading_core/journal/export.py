"""``journal.csv`` (spec 0024 AC-3 to AC-5): the columns, value formatting, EUR rounding, and the rate check.

Pure: rows come in through the ``JournalEntry`` shape (``trading_core.recording.queries.JournalRow``
fits it), lines go out to any text sink the caller passes. The CSV is RFC 4180, which is the
``csv`` module's default ``excel`` dialect: comma separated, ``"`` quoting only where needed, CRLF
line ends.
"""

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Final, Protocol
from uuid import UUID

from trading_core.journal.bounds import AMSTERDAM, JournalRange

JOURNAL_COLUMNS: Final = (
    "env",
    "kind",
    "mode",
    "at_utc",
    "at_amsterdam",
    "instrument_id",
    "side",
    "quantity",
    "price",
    "liquidity",
    "amount",
    "currency",
    "fx_rate",
    "fx_rate_date",
    "eur_amount",
    "strategy",
    "strategy_version",
    "run_id",
    "position_id",
    "source_id",
)
_USD: Final = "USD"
_HALF: Final = Fraction(1, 2)


class JournalEntry(Protocol):
    """The fields of one journal row the export reads (``JournalRow`` has them all)."""

    @property
    def kind(self) -> str: ...
    @property
    def at(self) -> datetime: ...
    @property
    def mode(self) -> str: ...
    @property
    def instrument_id(self) -> str: ...
    @property
    def side(self) -> str | None: ...
    @property
    def quantity(self) -> Decimal | None: ...
    @property
    def price(self) -> Decimal | None: ...
    @property
    def liquidity(self) -> str | None: ...
    @property
    def amount(self) -> Decimal: ...
    @property
    def currency(self) -> str: ...
    @property
    def fx_rate(self) -> Decimal | None: ...
    @property
    def fx_rate_date(self) -> date | None: ...
    @property
    def fx_missing(self) -> bool: ...
    @property
    def strategy(self) -> str: ...
    @property
    def strategy_version(self) -> str: ...
    @property
    def run_id(self) -> UUID: ...
    @property
    def position_id(self) -> UUID | None: ...
    @property
    def source_id(self) -> UUID: ...


class TextSink(Protocol):
    """Anything with ``write(str)``: a ``StringIO``, an open text file."""

    def write(self, text: str, /) -> object: ...


class JournalRefusedError(Exception):
    """Nothing may be exported; ``reason`` is the log code, the message is shown to you."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def eur_cents(amount: Decimal, rate: Decimal) -> Decimal:
    """``amount ÷ rate`` computed exactly, rounded once to 0.01 half away from zero (AC-4): ``-0.005`` → ``-0.01``."""
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    hundredths = Fraction(amount) / Fraction(rate) * 100
    size = abs(hundredths)
    whole = size.numerator // size.denominator
    if size - whole >= _HALF:
        whole += 1
    return Decimal(whole if hundredths >= 0 else -whole).scaleb(-2)


def plain(value: Decimal) -> str:
    """A Decimal in plain notation with a dot, never an exponent, at its own precision."""
    return format(value, "f")


def utc_text(at: datetime) -> str:
    """``2026-10-30T12:02:11.123456Z``."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def amsterdam_text(at: datetime) -> str:
    """``2026-10-30T13:02:11.123456+01:00``."""
    return at.astimezone(AMSTERDAM).isoformat(timespec="microseconds")


def _text(value: Decimal | UUID | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return plain(value)
    return str(value)


def row_eur(row: JournalEntry) -> Decimal:
    """The row's rounded EUR amount; a row without a rate never reaches the file (``RateCheck``)."""
    if row.fx_rate is None:
        raise ValueError(f"journal row {row.source_id} has no rate")
    return eur_cents(row.amount, row.fx_rate)


def journal_line(env: str, row: JournalEntry) -> list[str]:
    """One ``journal.csv`` line in ``JOURNAL_COLUMNS`` order; a field that does not apply is empty."""
    return [
        env,
        str(row.kind),
        str(row.mode),
        utc_text(row.at),
        amsterdam_text(row.at),
        row.instrument_id,
        _text(row.side),
        _text(row.quantity),
        _text(row.price),
        _text(row.liquidity),
        plain(row.amount),
        row.currency,
        _text(row.fx_rate),
        "" if row.fx_rate_date is None else row.fx_rate_date.isoformat(),
        plain(row_eur(row)),
        row.strategy,
        row.strategy_version,
        str(row.run_id),
        _text(row.position_id),
        str(row.source_id),
    ]


def write_header(sink: TextSink, columns: Sequence[str] = JOURNAL_COLUMNS) -> None:
    """The header line."""
    csv.writer(sink).writerow(columns)


def write_rows(sink: TextSink, env: str, rows: Iterable[JournalEntry]) -> int:
    """One line per row; returns how many."""
    writer = csv.writer(sink)
    count = 0
    for row in rows:
        writer.writerow(journal_line(env, row))
        count += 1
    return count


def journal_filename(env: str, journal_range: JournalRange, file: str = "journal") -> str:
    """``kfs-journal-<env>-<mode>-<start>-<end>.csv``, or ``kfs-journal-summary-…`` for the summary."""
    name = "kfs-journal" if file == "journal" else f"kfs-journal-{file}"
    return f"{name}-{env}-{journal_range.mode}-{journal_range.start}-{journal_range.end}.csv"


@dataclass
class RateCheck:
    """Collects why rows or bounds lack a final ECB rate (AC-5); ``refusal`` names the first problem.

    A row in a currency other than USD, a USD row with no rate on or before its UTC date, and a
    row or bound whose UTC date is past ``newest`` (its rate is not published yet) all refuse.
    """

    newest: date | None
    journal_range: JournalRange

    def __post_init__(self) -> None:
        self._currencies: set[str] = set()
        bounds = (self.journal_range.start_utc.date(), self.journal_range.end_utc.date())
        self._dates: set[date] = {day for day in bounds if self._unpublished(day)}

    def _unpublished(self, day: date) -> bool:
        return self.newest is None or day > self.newest

    def feed(self, rows: Iterable[JournalEntry]) -> None:
        """Check one chunk of rows."""
        for row in rows:
            day = row.at.astimezone(UTC).date()
            if row.currency != _USD:
                self._currencies.add(row.currency)
            elif row.fx_missing or self._unpublished(day):
                self._dates.add(day)

    def refusal(self) -> JournalRefusedError | None:
        """The error to refuse with, or None when every row and bound has its final rate."""
        if self._currencies:
            return JournalRefusedError("fx_missing", f"Currency {min(self._currencies)} has no EUR rate.")
        if self._dates:
            return JournalRefusedError(
                "fx_missing",
                f"No ECB rate for {min(self._dates)} yet. The alerter fetches it hourly, so try again later.",
            )
        return None
