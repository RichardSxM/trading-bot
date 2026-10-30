"""covers: spec 0024 AC-3 (columns, order, RFC 4180 CSV), AC-4 (plain decimals, the EUR amount rounded once half up,
both time columns, ids, empty fields), AC-5 (the rate check)."""

import csv
import io
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from trading_core.journal.bounds import parse_range
from trading_core.journal.export import (
    JOURNAL_COLUMNS,
    RateCheck,
    eur_cents,
    journal_filename,
    journal_line,
    plain,
    write_header,
    write_rows,
)

RUN = UUID("01a18cf3-361b-75f1-95d4-7f70591d723a")
POSITION = UUID("01a18cf3-35ea-70fd-aef0-09c1354b915d")
SOURCE = UUID("01a18cf3-3600-7000-8000-00000000abcd")
RANGE = parse_range("2026-10-01", "2026-10-29", "paper", date(2026, 10, 30))


@dataclass(frozen=True, kw_only=True)
class Row:
    kind: str = "fill"
    at: datetime = datetime(2026, 10, 27, 12, 2, 11, 123456, tzinfo=UTC)
    mode: str = "paper"
    instrument_id: str = "PF_XBTUSD.KRAKEN"
    side: str | None = "buy"
    quantity: Decimal | None = Decimal("0.0010")
    price: Decimal | None = Decimal("64210.5")
    liquidity: str | None = "maker"
    amount: Decimal = Decimal("-64.2105")
    currency: str = "USD"
    fx_rate: Decimal | None = Decimal("1.1625")
    fx_rate_date: date | None = date(2026, 10, 27)
    fx_missing: bool = False
    strategy: str = "donchian_scalp"
    strategy_version: str = "ab12cd"
    run_id: UUID = RUN
    position_id: UUID | None = POSITION
    source_id: UUID = SOURCE


@pytest.mark.parametrize(
    ("amount", "rate", "eur"),
    [
        ("-0.005", "1", "-0.01"),  # half away from zero
        ("0.005", "1", "0.01"),
        ("0.0049999", "1", "0.00"),
        ("-0.0049999", "1", "0.00"),  # never a negative zero
        ("-64.2105", "1.1625", "-55.23"),  # -55.2348...
        ("1", "3", "0.33"),
        ("2", "3", "0.67"),
        ("0.0116250", "1.1625", "0.01"),  # exactly 0.01
        ("0.00581250", "1.1625", "0.01"),  # exactly 0.005: rounded once, up
        ("-0.00581250", "1.1625", "-0.01"),
        ("123456789.12", "1", "123456789.12"),
    ],
)
def test_eur_is_exact_division_rounded_once_half_away_from_zero(amount, rate, eur):
    result = eur_cents(Decimal(amount), Decimal(rate))

    assert str(result) == eur
    assert format(result, "f") == eur


def test_eur_needs_a_positive_rate():
    with pytest.raises(ValueError, match="positive"):
        eur_cents(Decimal(1), Decimal(0))


def test_decimals_are_plain_at_their_own_precision():
    assert plain(Decimal("1E-8")) == "0.00000001"
    assert plain(Decimal("1E+3")) == "1000"
    assert plain(Decimal("0.00100")) == "0.00100"


def test_a_fill_line_has_every_column_in_order():
    line = journal_line("demo", Row())

    assert dict(zip(JOURNAL_COLUMNS, line, strict=True)) == {
        "env": "demo",
        "kind": "fill",
        "mode": "paper",
        "at_utc": "2026-10-27T12:02:11.123456Z",
        "at_amsterdam": "2026-10-27T13:02:11.123456+01:00",  # CET after 25 October
        "instrument_id": "PF_XBTUSD.KRAKEN",
        "side": "buy",
        "quantity": "0.0010",
        "price": "64210.5",
        "liquidity": "maker",
        "amount": "-64.2105",
        "currency": "USD",
        "fx_rate": "1.1625",
        "fx_rate_date": "2026-10-27",
        "eur_amount": "-55.23",
        "strategy": "donchian_scalp",
        "strategy_version": "ab12cd",
        "run_id": "01a18cf3-361b-75f1-95d4-7f70591d723a",
        "position_id": "01a18cf3-35ea-70fd-aef0-09c1354b915d",
        "source_id": "01a18cf3-3600-7000-8000-00000000abcd",
    }


def test_a_summer_time_has_the_plus_two_offset():
    line = journal_line("demo", Row(at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC)))

    assert line[3:5] == ["2026-07-01T00:00:00.000000Z", "2026-07-01T02:00:00.000000+02:00"]


def test_fields_that_do_not_apply_are_empty():
    funding = Row(kind="funding", side=None, quantity=None, price=None, liquidity=None, position_id=None)

    line = dict(zip(JOURNAL_COLUMNS, journal_line("demo", funding), strict=True))

    assert [line[name] for name in ("side", "quantity", "price", "liquidity", "position_id")] == [""] * 5


def test_a_row_without_a_rate_never_reaches_the_file():
    with pytest.raises(ValueError, match="no rate"):
        journal_line("demo", Row(fx_rate=None, fx_rate_date=None, fx_missing=True))


def test_the_file_is_rfc_4180_with_crlf_and_minimal_quoting():
    sink = io.StringIO()
    write_header(sink)
    count = write_rows(sink, "demo", [Row(), Row(strategy='odd, "name"')])

    text = sink.getvalue()
    assert count == 2
    assert text.startswith(",".join(JOURNAL_COLUMNS) + "\r\n")
    assert text.count("\r\n") == 3
    assert '"odd, ""name"""' in text
    assert "\n" not in text.replace("\r\n", "")
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed[2][JOURNAL_COLUMNS.index("strategy")] == 'odd, "name"'


def test_filenames_name_env_mode_and_dates():
    assert journal_filename("demo", RANGE) == "kfs-journal-demo-paper-2026-10-01-2026-10-29.csv"
    assert journal_filename("live", RANGE, "summary") == "kfs-journal-summary-live-paper-2026-10-01-2026-10-29.csv"


def test_the_rate_check_passes_when_every_row_and_bound_has_its_rate():
    check = RateCheck(date(2026, 10, 29), RANGE)
    check.feed([Row(), Row(kind="funding")])

    assert check.refusal() is None


def test_a_non_usd_row_names_its_currency():
    check = RateCheck(date(2026, 10, 29), RANGE)
    check.feed([Row(), Row(kind="fee", currency="BTC", fx_rate=None, fx_rate_date=None, fx_missing=True)])

    refusal = check.refusal()

    assert refusal is not None
    assert (refusal.reason, str(refusal)) == ("fx_missing", "Currency BTC has no EUR rate.")


def test_a_usd_row_with_no_rate_names_the_earliest_such_utc_date():
    check = RateCheck(date(2026, 10, 29), RANGE)
    missing = replace(Row(), fx_rate=None, fx_rate_date=None, fx_missing=True)
    check.feed(
        [replace(missing, at=datetime(2026, 10, 3, tzinfo=UTC)), replace(missing, at=datetime(2026, 10, 2, tzinfo=UTC))]
    )

    refusal = check.refusal()

    assert refusal is not None
    assert str(refusal) == "No ECB rate for 2026-10-02 yet. The alerter fetches it hourly, so try again later."


def test_an_end_bound_past_the_newest_rate_refuses_even_with_no_rows():
    check = RateCheck(date(2026, 10, 23), RANGE)  # a Friday; the range ends on Thursday the 29th

    refusal = check.refusal()

    assert refusal is not None
    assert "2026-10-29" in str(refusal)


def test_no_rates_at_all_refuses():
    refusal = RateCheck(None, RANGE).refusal()

    assert refusal is not None
    assert "2026-09-30" in str(refusal)  # the start bound's UTC date: 22:00 UTC on 30 September
