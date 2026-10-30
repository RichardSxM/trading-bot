"""covers: spec 0024 AC-15 (the ECB csvdata parser, rejected whole on any bad line), AC-18 (a fixture, no network)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading_core.fx.ecb import FxParseError, ecb_url, parse_ecb_csv

# A real Data Portal response for 2026-10-01 to 2026-10-14, captured once on 2026-10-30.
FIXTURE = Path(__file__).parent / "fixtures" / "ecb_usd_eur_2026-10-01_2026-10-14.csv"
START, END = date(2026, 10, 1), date(2026, 10, 14)
HEADER = "KEY,FREQ,TIME_PERIOD,OBS_VALUE\r\n"


def _body(*lines: str) -> str:
    return HEADER + "".join(f"EXR.D.USD.EUR.SP00.A,D,{line}\r\n" for line in lines)


def test_the_fixture_gives_one_row_per_business_day_in_date_order():
    rows = parse_ecb_csv(FIXTURE.read_text(encoding="utf-8"), START, END)

    assert [row.rate_date for row in rows] == [
        date(2026, 10, day) for day in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)
    ]  # no weekend rows
    assert rows[0].rate == Decimal("1.1298")
    assert rows[-1].rate == Decimal("1.1195")
    assert {(row.base_currency, row.quote_currency, row.source) for row in rows} == {("EUR", "USD", "ECB")}


def test_rates_are_decimals_parsed_from_the_text_never_floats():
    (row,) = parse_ecb_csv(_body("2026-10-01,1.10000"), START, END)

    assert isinstance(row.rate, Decimal)
    assert str(row.rate) == "1.10000"  # the published precision is kept


def test_columns_are_found_by_name_in_any_order():
    text = "OBS_VALUE,TIME_PERIOD\n1.2,2026-10-02\n"

    assert [(row.rate_date, row.rate) for row in parse_ecb_csv(text, START, END)] == [
        (date(2026, 10, 2), Decimal("1.2"))
    ]


def test_a_header_only_body_is_zero_rows():
    assert parse_ecb_csv(HEADER, START, END) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "KEY,TIME_PERIOD\r\nx,2026-10-01\r\n",
        "KEY,OBS_VALUE\r\nx,1.1\r\n",
        "<html>Service unavailable</html>",
    ],
    ids=["empty", "no rate column", "no date column", "html"],
)
def test_a_body_without_both_columns_is_rejected(text):
    with pytest.raises(FxParseError, match="missing column"):
        parse_ecb_csv(text, START, END)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("2026-10-01,NaN", "not a plain decimal"),
        ("2026-10-01,", "not a plain decimal"),
        ("2026-10-01,1e0", "not a plain decimal"),
        ("2026-10-01,-1.1", "not a plain decimal"),
        ("2026-10-01,0.0000", "not positive"),
        ("01/10/2026,1.1", "not YYYY-MM-DD"),
        ("20261001,1.1", "not YYYY-MM-DD"),
        ("2026-09-30,1.1", "outside the request"),
        ("2026-10-15,1.1", "outside the request"),
    ],
)
def test_one_bad_line_rejects_the_whole_body(line, reason):
    with pytest.raises(FxParseError, match=reason):
        parse_ecb_csv(_body("2026-10-02,1.12", line), START, END)


def test_a_repeated_date_is_rejected():
    with pytest.raises(FxParseError, match="appears twice"):
        parse_ecb_csv(_body("2026-10-02,1.12", "2026-10-02,1.12"), START, END)


def test_a_line_with_a_missing_field_is_rejected():
    with pytest.raises(FxParseError, match="field count"):
        parse_ecb_csv(HEADER + "EXR.D.USD.EUR.SP00.A,D,2026-10-02\r\n", START, END)


def test_a_line_with_an_extra_field_is_rejected():
    with pytest.raises(FxParseError, match="field count"):
        parse_ecb_csv(_body("2026-10-02,1.12,extra"), START, END)


def test_the_url_asks_for_csvdata_over_the_inclusive_range():
    url = ecb_url("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A", START, END)

    assert url == (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
        "?startPeriod=2026-10-01&endPeriod=2026-10-14&format=csvdata"
    )
