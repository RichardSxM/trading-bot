"""The check panel and ``summary.csv`` (spec 0024 AC-7 to AC-11): totals, balances, the P&L check, summary rows.

Pure. One ``JournalSummary`` feeds both the panel and ``summary.csv``, so the two always agree
(AC-11), and its EUR totals are sums of the same rounded ``eur_amount`` values ``journal.csv``
holds, so a spreadsheet sum of that column gives exactly ``eur net`` (AC-7).
"""

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from trading_core.journal.bounds import TIME_ZONE, JournalRange
from trading_core.journal.export import JournalEntry, TextSink, eur_cents, plain, row_eur, utc_text
from trading_core.recording.enums import RunMode

SUMMARY_COLUMNS: Final = ("section", "item", "currency", "value")
TOTAL_ITEMS: Final = ("trading", "fees", "funding", "net")
PAPER_NOTE: Final = "paper trading, not real trades"
EUR: Final = "EUR"
USD: Final = "USD"
CENT: Final = Decimal("0.01")
_ITEM_OF_KIND: Final = {"fill": "trading", "fee": "fees", "funding": "funding"}
_ZERO_CENTS: Final = Decimal("0.00")


@dataclass(frozen=True)
class Totals:
    """One currency's sums by kind; ``net`` is their sum."""

    trading: Decimal
    fees: Decimal
    funding: Decimal

    @property
    def net(self) -> Decimal:
        """``trading + fees + funding``."""
        return self.trading + self.fees + self.funding

    def items(self) -> list[tuple[str, Decimal]]:
        """The four figures in ``TOTAL_ITEMS`` order."""
        return list(zip(TOTAL_ITEMS, (self.trading, self.fees, self.funding, self.net), strict=True))


@dataclass
class JournalTotals:
    """Accumulates AC-7's totals and AC-9's unlinked rows over journal rows, one chunk at a time.

    Native sums keep full precision; the EUR sums add each row's rounded ``eur_amount``. USD is
    always listed, even with no rows, since it is the settle currency.
    """

    rows: int = 0
    unlinked_rows: int = 0
    unlinked_net: Decimal = Decimal(0)
    _native: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    _eur: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(_ITEM_OF_KIND.values(), _ZERO_CENTS))

    def feed(self, rows: Iterable[JournalEntry]) -> None:
        """Add one chunk; every row must have its rate (``RateCheck`` passed)."""
        for row in rows:
            item = _ITEM_OF_KIND[str(row.kind)]
            native = self._native.setdefault(row.currency, dict.fromkeys(_ITEM_OF_KIND.values(), Decimal(0)))
            native[item] += row.amount
            self._eur[item] += row_eur(row)
            self.rows += 1
            if row.position_id is None:
                self.unlinked_rows += 1
                if row.currency == USD:
                    self.unlinked_net += row.amount

    def native(self) -> dict[str, Totals]:
        """Per native currency, sorted, USD always present."""
        sums = {USD: dict.fromkeys(_ITEM_OF_KIND.values(), Decimal(0)), **self._native}
        return {currency: Totals(**sums[currency]) for currency in sorted(sums)}

    def eur(self) -> Totals:
        """The sums of the rounded EUR amounts."""
        return Totals(**self._eur)


@dataclass(frozen=True)
class Snapshot:
    """The balance snapshot used for one ``(account_ref, currency)`` at a bound (AC-8)."""

    account_ref: str
    currency: str
    balance_total: Decimal
    captured_at: datetime
    run_id: UUID | None


@dataclass(frozen=True)
class BalanceLine:
    """One account currency at a bound; ``snapshot`` None is "none recorded", ``eur`` None is blank."""

    account_ref: str
    currency: str
    snapshot: Snapshot | None
    eur: Decimal | None


def balance_lines(
    snapshots: Iterable[Snapshot], pairs: Iterable[tuple[str, str]], rate: Decimal | None
) -> list[BalanceLine]:
    """One line per ``(account_ref, currency)`` in ``pairs``, sorted, with its EUR value (AC-8).

    USD converts at ``rate`` (the bound's ECB rate), EUR at 1, rounded like AC-4; any other
    currency, or USD without a rate, gets a blank EUR value.
    """
    found = {(s.account_ref, s.currency): s for s in snapshots}
    lines = []
    for account_ref, currency in sorted(set(pairs)):
        snapshot = found.get((account_ref, currency))
        eur = None
        if snapshot is not None:
            if currency == EUR:
                eur = eur_cents(snapshot.balance_total, Decimal(1))
            elif currency == USD and rate is not None:
                eur = eur_cents(snapshot.balance_total, rate)
        lines.append(BalanceLine(account_ref, currency, snapshot, eur))
    return lines


@dataclass(frozen=True)
class PositionFigures:
    """AC-9's dashboard side: closed positions in range and positions crossing a range edge."""

    dashboard_net: Decimal
    counted: int
    crossing: int


class CheckStatus(StrEnum):
    """The P&L check's verdict (AC-9)."""

    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True)
class PnlCheck:
    """``journal net`` against ``dashboard net`` (AC-9); ``reason`` is ``edge``, ``unlinked``, or empty."""

    status: CheckStatus
    reason: str
    journal_net: Decimal
    dashboard_net: Decimal
    tolerance: Decimal
    counted: int
    crossing: int
    unlinked_rows: int
    unlinked_net: Decimal

    @property
    def difference(self) -> Decimal:
        """``journal net - dashboard net``."""
        return self.journal_net - self.dashboard_net


def pnl_check(totals: JournalTotals, positions: PositionFigures) -> PnlCheck:
    """The verdict: ``not_comparable`` when a position crosses an edge (named first) or a row is unlinked,
    else ``match`` within 0.01 USD per position counted (at least one), else ``mismatch``."""
    journal_net = totals.native()[USD].net
    tolerance = CENT * max(1, positions.counted)
    if positions.crossing:
        status, reason = CheckStatus.NOT_COMPARABLE, "edge"
    elif totals.unlinked_rows:
        status, reason = CheckStatus.NOT_COMPARABLE, "unlinked"
    elif abs(journal_net - positions.dashboard_net) <= tolerance:
        status, reason = CheckStatus.MATCH, ""
    else:
        status, reason = CheckStatus.MISMATCH, ""
    return PnlCheck(
        status=status,
        reason=reason,
        journal_net=journal_net,
        dashboard_net=positions.dashboard_net,
        tolerance=tolerance,
        counted=positions.counted,
        crossing=positions.crossing,
        unlinked_rows=totals.unlinked_rows,
        unlinked_net=totals.unlinked_net,
    )


@dataclass(frozen=True)
class JournalSummary:
    """Everything the panel and ``summary.csv`` show for one range (AC-7 to AC-11)."""

    journal_range: JournalRange
    rows: int
    native: dict[str, Totals]
    eur: Totals
    balance_start: list[BalanceLine]
    balance_end: list[BalanceLine]
    check: PnlCheck


def summarize(
    journal_range: JournalRange,
    totals: JournalTotals,
    start: tuple[Sequence[Snapshot], Decimal | None],
    end: tuple[Sequence[Snapshot], Decimal | None],
    positions: PositionFigures,
) -> JournalSummary:
    """The summary from the fed totals, each bound's snapshots and rate, and the position figures.

    Both bounds list every account currency seen at either, so one recorded only later shows
    "none recorded" at the start.
    """
    pairs = {(s.account_ref, s.currency) for s in (*start[0], *end[0])}
    return JournalSummary(
        journal_range=journal_range,
        rows=totals.rows,
        native=totals.native(),
        eur=totals.eur(),
        balance_start=balance_lines(start[0], pairs, start[1]),
        balance_end=balance_lines(end[0], pairs, end[1]),
        check=pnl_check(totals, positions),
    )


def _opt(value: Decimal | None) -> str:
    return "" if value is None else plain(value)


def _balance_rows(section: str, lines: Iterable[BalanceLine]) -> list[tuple[str, str, str, str]]:
    rows = []
    for line in lines:
        snapshot = line.snapshot
        rows += [
            (section, line.account_ref, line.currency, "" if snapshot is None else plain(snapshot.balance_total)),
            (section, line.account_ref, EUR, _opt(line.eur)),
            (
                section,
                f"{line.account_ref}.captured_at",
                "",
                "" if snapshot is None else utc_text(snapshot.captured_at),
            ),
            (
                section,
                f"{line.account_ref}.run_id",
                "",
                "" if snapshot is None or snapshot.run_id is None else str(snapshot.run_id),
            ),
        ]
    return rows


def summary_rows(
    env: str, summary: JournalSummary, generated_at: datetime, git_commit: str
) -> list[tuple[str, str, str, str]]:
    """``summary.csv``'s rows after its header, in AC-10's order."""
    r, check = summary.journal_range, summary.check
    rows = [
        ("range", "env", "", env),
        ("range", "mode", "", r.mode.value),
        ("range", "start", "", r.start.isoformat()),
        ("range", "end", "", r.end.isoformat()),
        ("range", "start_utc", "", utc_text(r.start_utc)),
        ("range", "end_utc", "", utc_text(r.end_utc)),
        ("range", "time_zone", "", TIME_ZONE),
        ("range", "generated_at_utc", "", utc_text(generated_at)),
        ("range", "git_commit", "", git_commit),
        ("range", "rows", "", str(summary.rows)),
    ]
    if r.mode is RunMode.PAPER:
        rows.append(("range", "note", "", PAPER_NOTE))
    for currency, totals in summary.native.items():
        rows += [("totals", item, currency, plain(value)) for item, value in totals.items()]
    rows += [("totals", item, EUR, plain(value)) for item, value in summary.eur.items()]
    rows += _balance_rows("balance_start", summary.balance_start)
    rows += _balance_rows("balance_end", summary.balance_end)
    rows += [
        ("check", "status", "", check.status.value),
        ("check", "reason", "", check.reason),
        ("check", "journal_net", USD, plain(check.journal_net)),
        ("check", "dashboard_net", USD, plain(check.dashboard_net)),
        ("check", "difference", USD, plain(check.difference)),
        ("check", "tolerance", USD, plain(check.tolerance)),
        ("check", "positions_counted", "", str(check.counted)),
        ("check", "positions_crossing_edge", "", str(check.crossing)),
        ("check", "unlinked_rows", "", str(check.unlinked_rows)),
        ("check", "unlinked_net", USD, plain(check.unlinked_net)),
    ]
    return rows


def write_summary(sink: TextSink, env: str, summary: JournalSummary, generated_at: datetime, git_commit: str) -> None:
    """``summary.csv`` in the same dialect as ``journal.csv``."""
    writer = csv.writer(sink)
    writer.writerow(SUMMARY_COLUMNS)
    writer.writerows(summary_rows(env, summary, generated_at, git_commit))
