"""covers: spec 0024 AC-7 (totals per currency and in EUR from the rounded row amounts), AC-8 (balances in EUR, none
recorded, other currencies blank), AC-9 (the check verdict and its tolerance), AC-10 (summary.csv rows and order),
AC-11 (the EUR net equals the sum of journal.csv's eur_amount column)."""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from trading_core.journal.bounds import parse_range
from trading_core.journal.export import write_header, write_rows
from trading_core.journal.summary import (
    SUMMARY_COLUMNS,
    CheckStatus,
    JournalTotals,
    PositionFigures,
    Snapshot,
    balance_lines,
    pnl_check,
    summarize,
    summary_rows,
    write_summary,
)

RUN = UUID("01a18cf3-361b-75f1-95d4-7f70591d723a")
POSITION = UUID("01a18cf3-35ea-70fd-aef0-09c1354b915d")
SOURCE = UUID("01a18cf3-3600-7000-8000-00000000abcd")
PAPER = parse_range("2026-10-01", "2026-10-29", "paper", date(2026, 10, 30))
LIVE = parse_range("2026-10-01", "2026-10-29", "live", date(2026, 10, 30))
AT = datetime(2026, 10, 27, 12, tzinfo=UTC)
GENERATED = datetime(2026, 10, 30, 9, 15, tzinfo=UTC)


@dataclass(frozen=True, kw_only=True)
class Row:
    kind: str = "fill"
    at: datetime = AT
    mode: str = "paper"
    instrument_id: str = "PF_XBTUSD.KRAKEN"
    side: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    liquidity: str | None = None
    amount: Decimal = Decimal(0)
    currency: str = "USD"
    fx_rate: Decimal | None = Decimal("1.1625")
    fx_rate_date: date | None = date(2026, 10, 27)
    fx_missing: bool = False
    strategy: str = "donchian_scalp"
    strategy_version: str = "ab12cd"
    run_id: UUID = RUN
    position_id: UUID | None = POSITION
    source_id: UUID = SOURCE


ROWS = [
    Row(kind="fill", amount=Decimal("-64.2105")),
    Row(kind="fee", amount=Decimal("0.0128")),  # a maker rebate is money in
    Row(kind="fill", amount=Decimal("64.3105")),
    Row(kind="fee", amount=Decimal("-0.0386")),
    Row(kind="funding", amount=Decimal("-0.0051"), position_id=None),
]


def _fed(rows=ROWS):
    totals = JournalTotals()
    totals.feed(rows[:2])  # two chunks, like two months
    totals.feed(rows[2:])
    return totals


def _positions(net="0.0691", counted=1, crossing=0):
    return PositionFigures(dashboard_net=Decimal(net), counted=counted, crossing=crossing)


def test_native_totals_keep_full_precision_and_net_is_their_sum():
    usd = _fed().native()["USD"]

    assert (usd.trading, usd.fees, usd.funding) == (Decimal("0.1000"), Decimal("-0.0258"), Decimal("-0.0051"))
    assert usd.net == Decimal("0.0691")
    assert [item for item, _ in usd.items()] == ["trading", "fees", "funding", "net"]


def test_eur_totals_sum_the_rounded_rows_so_the_csv_column_adds_up():
    totals = _fed()
    sink = io.StringIO()
    write_header(sink)
    write_rows(sink, "demo", ROWS)
    column = sum(Decimal(line["eur_amount"]) for line in csv.DictReader(io.StringIO(sink.getvalue(), newline="")))

    eur = totals.eur()

    assert (eur.trading, eur.fees, eur.funding) == (Decimal("0.09"), Decimal("-0.02"), Decimal("-0.00"))
    assert eur.net == column
    assert totals.rows == 5


def test_usd_is_listed_with_no_rows():
    totals = JournalTotals()

    assert totals.native()["USD"].net == 0
    assert str(totals.eur().net) == "0.00"


def test_unlinked_rows_are_counted_with_their_usd_sum():
    totals = _fed()

    assert (totals.unlinked_rows, totals.unlinked_net) == (1, Decimal("-0.0051"))


def _snapshot(currency="USD", total="1000.50", account="paper-1"):
    return Snapshot(account, currency, Decimal(total), AT, RUN)


def test_balances_convert_usd_at_the_rate_and_eur_at_one_and_leave_others_blank():
    snapshots = [_snapshot(), _snapshot("EUR", "10.005"), _snapshot("BTC", "0.5")]
    pairs = [(s.account_ref, s.currency) for s in snapshots]

    lines = balance_lines(snapshots, pairs, Decimal("1.1625"))

    assert [(line.currency, line.eur) for line in lines] == [
        ("BTC", None),
        ("EUR", Decimal("10.01")),
        ("USD", Decimal("860.65")),  # 1000.50 / 1.1625 = 860.645...
    ]


def test_a_pair_with_no_snapshot_at_the_start_is_none_recorded():
    later = _snapshot(account="paper-2")
    summary = summarize(
        PAPER, JournalTotals(), ([_snapshot()], Decimal("1.2")), ([_snapshot(), later], None), _positions()
    )

    assert [(line.account_ref, line.snapshot) for line in summary.balance_start] == [
        ("paper-1", _snapshot()),
        ("paper-2", None),
    ]
    assert summary.balance_end[1].eur is None  # USD without a rate is blank, never a refusal


@pytest.mark.parametrize(
    ("net", "counted", "crossing", "rows", "status", "reason"),
    [
        ("0.0742", 1, 0, ROWS[:4], CheckStatus.MATCH, ""),  # journal net 0.0742
        ("0.0842", 1, 0, ROWS[:4], CheckStatus.MATCH, ""),  # exactly the tolerance
        ("0.0843", 1, 0, ROWS[:4], CheckStatus.MISMATCH, ""),
        ("0.1043", 3, 0, ROWS[:4], CheckStatus.MISMATCH, ""),  # 0.03 tolerance for three
        ("0.1042", 3, 0, ROWS[:4], CheckStatus.MATCH, ""),
        ("5", 1, 1, ROWS[:4], CheckStatus.NOT_COMPARABLE, "edge"),
        ("0.0742", 1, 0, ROWS, CheckStatus.NOT_COMPARABLE, "unlinked"),
        ("0.0742", 1, 2, ROWS, CheckStatus.NOT_COMPARABLE, "edge"),  # an edge is named first
    ],
)
def test_the_check_verdict(net, counted, crossing, rows, status, reason):
    totals = JournalTotals()
    totals.feed(rows)

    check = pnl_check(totals, _positions(net, counted, crossing))

    assert (check.status, check.reason) == (status, reason)
    assert check.tolerance == Decimal("0.01") * max(1, counted)
    assert check.difference == check.journal_net - check.dashboard_net


def test_no_positions_still_has_a_one_cent_tolerance():
    check = pnl_check(JournalTotals(), _positions("0.01", 0))

    assert (check.status, check.tolerance) == (CheckStatus.MATCH, Decimal("0.01"))


def _rows_of(summary, env="demo"):
    return summary_rows(env, summary, GENERATED, "abc1234")


def test_summary_rows_follow_ac_10():
    summary = summarize(PAPER, _fed(), ([_snapshot()], Decimal("1.25")), ([], None), _positions("0.0742"))
    rows = _rows_of(summary)
    by_key = {(s, i, c): v for s, i, c, v in rows}

    assert [r[:2] for r in rows[:11]] == [
        ("range", item)
        for item in (
            "env",
            "mode",
            "start",
            "end",
            "start_utc",
            "end_utc",
            "time_zone",
            "generated_at_utc",
            "git_commit",
            "rows",
            "note",
        )
    ]
    assert by_key["range", "start_utc", ""] == "2026-09-30T22:00:00.000000Z"
    assert by_key["range", "time_zone", ""] == "Europe/Amsterdam"
    assert by_key["range", "note", ""] == "paper trading, not real trades"
    assert by_key["range", "rows", ""] == "5"
    assert by_key["totals", "net", "USD"] == "0.0691"
    assert by_key["totals", "net", "EUR"] == "0.07"
    assert by_key["balance_start", "paper-1", "USD"] == "1000.50"
    assert by_key["balance_start", "paper-1", "EUR"] == "800.40"
    assert by_key["balance_start", "paper-1.captured_at", ""] == "2026-10-27T12:00:00.000000Z"
    assert by_key["balance_start", "paper-1.run_id", ""] == str(RUN)
    assert by_key["balance_end", "paper-1", "USD"] == ""  # none recorded
    assert by_key["balance_end", "paper-1", "EUR"] == ""
    assert [r[1] for r in rows if r[0] == "check"] == [
        "status",
        "reason",
        "journal_net",
        "dashboard_net",
        "difference",
        "tolerance",
        "positions_counted",
        "positions_crossing_edge",
        "unlinked_rows",
        "unlinked_net",
    ]
    assert by_key["check", "status", ""] == "not_comparable"
    assert by_key["check", "reason", ""] == "unlinked"
    assert by_key["check", "difference", "USD"] == "-0.0051"


def test_live_has_no_paper_note():
    rows = _rows_of(summarize(LIVE, JournalTotals(), ([], None), ([], None), _positions("0", 0)), env="live")

    assert ("range", "note") not in [r[:2] for r in rows]


def test_write_summary_is_the_header_then_the_rows_in_the_csv_dialect():
    summary = summarize(PAPER, _fed(), ([], None), ([], None), _positions())
    sink = io.StringIO()

    write_summary(sink, "demo", summary, GENERATED, "abc1234")

    lines = sink.getvalue().split("\r\n")
    assert lines[0] == ",".join(SUMMARY_COLUMNS)
    assert lines[1] == "range,env,,demo"
    assert len(lines) - 2 == len(_rows_of(summary))  # a trailing CRLF
