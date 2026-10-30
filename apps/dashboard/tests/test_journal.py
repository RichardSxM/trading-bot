"""covers: spec 0024 AC-1 (the export form on Trades with its defaults, Check swaps the panel in, links after a
pass), AC-2 (bad ranges get our own 422 text, or a refused panel), AC-3 (the CSV response's type and filename), AC-5
and AC-6 (a refusal is a 409 with its message, or a panel without links), AC-7 to AC-9 (the panel's totals, balances,
and check states), AC-10 (summary.csv), AC-11 (the panel and summary.csv show the same figures), AC-12 (behind the
login, HX-Redirect for an HTMX Check, logged without amounts), AC-14 (a failure mid stream aborts the response)."""

import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from trading_core.journal.bounds import JournalRange, parse_range
from trading_core.journal.export import JournalRefusedError
from trading_core.journal.summary import JournalTotals, PositionFigures, Snapshot

from .conftest import Clock, FakeExport, log_in, make_app, summary_of

# Friday 30 October 2026, 10:00 UTC: yesterday in Amsterdam is the 29th.
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC).timestamp()
GOOD = "/trades/journal.csv?start=2026-10-01&end=2026-10-29&mode=paper"


@contextmanager
def _client(redis, open_export=None, env="demo", clock=None, read_summary=None):
    clock = clock or Clock(NOW)
    app = make_app(redis, clock, env=env, open_export=open_export, read_journal_summary=read_summary)
    with TestClient(app, follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        yield client


def _recording(opened):
    """An opener that records each range and streams an empty export."""

    def open_export(journal_range):
        opened.append(journal_range)
        return FakeExport()

    return open_export


def _codes(caplog):
    return [r.fields for r in caplog.records if "fields" in r.__dict__ and r.fields["code"].startswith("journal_")]


def test_the_trades_page_shows_the_form_with_its_defaults(redis):
    with _client(redis) as client:
        page = client.get("/trades").text

    assert "Export journal" in page
    assert page.index("Export journal") < page.index("Closed round trips")  # above the lists
    assert 'hx-get="/trades/journal/check"' in page
    assert 'hx-target="#journal-panel"' in page
    assert 'id="journal-panel"' in page
    assert 'name="start" type="date" value="2026-01-01"' in page
    assert 'name="end" type="date" value="2026-10-29"' in page
    assert '<option value="paper" selected>' in page
    assert '<option value="live">' in page


def test_the_default_mode_is_live_in_the_live_env(redis, clock):
    from .conftest import live_client

    with live_client(redis, clock) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        page = client.get("/trades").text

    assert '<option value="live" selected>' in page


def test_the_form_shows_even_when_the_lists_cannot_be_read(redis):
    def broken(page, fills_page):
        raise OperationalError("select", {}, Exception("down"))

    clock = Clock(NOW)
    with TestClient(make_app(redis, clock, read_trades=broken), follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        page = client.get("/trades").text

    assert "Trades unavailable." in page
    assert "Export journal" in page


def test_a_good_request_streams_the_csv_as_an_attachment(redis, caplog):
    caplog.set_level("INFO")
    opened: list[JournalRange] = []
    export = FakeExport(chunks=("env,kind\r\n", "demo,fill\r\n"), rows=1)

    def open_export(journal_range):
        opened.append(journal_range)
        return export

    with _client(redis, open_export) as client:
        response = client.get(GOOD)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="kfs-journal-demo-paper-2026-10-01-2026-10-29.csv"'
    )
    assert response.content == b"env,kind\r\ndemo,fill\r\n"  # UTF-8, no BOM
    [journal_range] = opened
    assert (journal_range.start, journal_range.end, journal_range.mode) == (
        date(2026, 10, 1),
        date(2026, 10, 29),
        "paper",
    )
    assert export.closed
    assert _codes(caplog) == [
        {
            "code": "journal_exported",
            "env": "demo",
            "mode": "paper",
            "start": "2026-10-01",
            "end": "2026-10-29",
            "file": "journal",
            "rows": 1,
        }
    ]


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-13-01&end=2026-10-29&mode=paper",
        "start=2026-10-01&end=2026-10-30&mode=paper",  # today
        "start=2026-10-01&end=2026-10-29&mode=both",
        "start=2026-10-29&end=2026-10-01&mode=paper",
        "end=2026-10-29&mode=paper",
        "",
    ],
    ids=["bad month", "today", "both", "reversed", "no start", "nothing"],
)
def test_a_bad_range_is_our_own_422_text_and_opens_nothing(redis, caplog, query):
    caplog.set_level("INFO")
    opened: list[JournalRange] = []

    with _client(redis, _recording(opened)) as client:
        response = client.get(f"/trades/journal.csv?{query}")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/plain")
    assert not response.text.startswith("{")  # never FastAPI's JSON
    assert opened == []
    assert _codes(caplog) == [{"code": "journal_export_refused", "reason": "bad_range"}]


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("fx_missing", "No ECB rate for 2026-10-29 yet. The alerter fetches it hourly, so try again later."),
        ("fx_missing", "Currency BTC has no EUR rate."),
        ("inverse_instrument", "the journal supports linear contracts only; inverse in range: ['FI_XBTUSD.KRAKEN']"),
    ],
)
def test_a_refusal_is_a_409_with_its_message(redis, caplog, reason, message):
    caplog.set_level("INFO")

    def refuse(journal_range):
        raise JournalRefusedError(reason, message)

    with _client(redis, refuse) as client:
        response = client.get(GOOD)

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == message
    assert "content-disposition" not in response.headers
    assert _codes(caplog) == [{"code": "journal_export_refused", "reason": reason}]


def test_postgres_down_before_the_first_byte_is_a_503(redis):
    def down(journal_range):
        raise OperationalError("select", {}, Exception("down"))

    with _client(redis, down) as client:
        response = client.get(GOOD)

    assert response.status_code == 503
    assert "cannot read Postgres" in response.text


def test_a_database_error_mid_stream_aborts_the_response(redis, caplog):
    caplog.set_level("INFO")
    export = FakeExport(chunks=("env,kind\r\n", "demo,fill\r\n"), error=OperationalError("select", {}, Exception("x")))

    with _client(redis, lambda r: export) as client, pytest.raises(OperationalError):
        client.get(GOOD)

    assert export.closed
    assert "journal_exported" not in [f["code"] for f in _codes(caplog)]


def test_the_csv_needs_a_session_and_sends_no_file(redis):
    opened: list[JournalRange] = []
    app = make_app(redis, Clock(NOW), open_export=_recording(opened))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(GOOD)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert opened == []


def test_the_log_lines_never_carry_an_amount(redis, caplog):
    caplog.set_level("INFO")
    with _client(redis, lambda r: FakeExport(rows=3)) as client:
        client.get(GOOD)

    for fields in _codes(caplog):
        assert set(fields) <= {"code", "env", "mode", "start", "end", "file", "rows", "reason"}


CHECK = "/trades/journal/check?start=2026-10-01&end=2026-10-29&mode=paper"
SUMMARY = "/trades/journal-summary.csv?start=2026-10-01&end=2026-10-29&mode=paper"
HTMX = {"HX-Request": "true"}
RANGE = parse_range("2026-10-01", "2026-10-29", "paper", date(2026, 10, 30))
RUN = UUID("01a18cf3-361b-75f1-95d4-7f70591d723a")


@dataclass(frozen=True, kw_only=True)
class Row:
    kind: str = "fill"
    at: datetime = datetime(2026, 10, 27, 12, tzinfo=UTC)
    mode: str = "paper"
    instrument_id: str = "PF_XBTUSD.KRAKEN"
    side: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    liquidity: str | None = None
    amount: Decimal = Decimal(0)
    currency: str = "USD"
    fx_rate: Decimal | None = Decimal("1.25")
    fx_rate_date: date | None = date(2026, 10, 27)
    fx_missing: bool = False
    strategy: str = "donchian_scalp"
    strategy_version: str = "ab12cd"
    run_id: UUID = RUN
    position_id: UUID | None = RUN
    source_id: UUID = RUN


def _summary(net="12.5", counted=2, crossing=0, unlinked=False):
    totals = JournalTotals()
    totals.feed(
        [
            Row(amount=Decimal("12.5100")),
            Row(kind="fee", amount=Decimal("-0.0100"), position_id=None if unlinked else RUN),
        ]
    )
    snapshot = Snapshot("KRAKEN-001", "USD", Decimal("1000.25"), datetime(2026, 9, 30, 21, tzinfo=UTC), RUN)
    return summary_of(
        RANGE,
        totals,
        ([snapshot], Decimal("1.25")),
        ((), Decimal("1.25")),
        PositionFigures(dashboard_net=Decimal(net), counted=counted, crossing=crossing),
    )


def test_check_renders_the_panel_with_both_download_links(redis):
    with _client(redis, read_summary=lambda r: _summary()) as client:
        response = client.get(CHECK, headers=HTMX)

    assert response.status_code == 200
    html = response.text
    assert "<html" not in html  # a partial, never the page
    assert 'badge--ok">match' in html
    assert 'href="/trades/journal.csv?start=2026-10-01&amp;end=2026-10-29&amp;mode=paper"' in html
    assert 'href="/trades/journal-summary.csv?start=2026-10-01&amp;end=2026-10-29&amp;mode=paper"' in html
    assert "12.5000" in html  # the net
    assert "1,000.25" in html  # a balance
    assert "800.20" in html  # its EUR value
    assert "none recorded" in html  # the account has no snapshot at the end
    assert "simulated accounts that reset" in html  # paper


def test_a_mismatch_warns_but_still_offers_the_files(redis):
    with _client(redis, read_summary=lambda r: _summary(net="12.6")) as client:
        html = client.get(CHECK, headers=HTMX).text

    assert "journal-panel--mismatch" in html
    assert "badge badge--warn" in html
    assert "journal.csv?" in html


@pytest.mark.parametrize(
    ("summary", "why"),
    [(_summary(crossing=1), "crossed a range edge"), (_summary(unlinked=True), "belong to no position")],
    ids=["edge", "unlinked"],
)
def test_not_comparable_says_why(redis, summary, why):
    with _client(redis, read_summary=lambda r: summary) as client:
        html = client.get(CHECK, headers=HTMX).text

    assert "not comparable" in html
    assert why in html
    assert "journal.csv?" in html


def test_a_bad_range_is_a_200_refused_panel_without_links(redis):
    with _client(redis) as client:
        response = client.get("/trades/journal/check?start=2026-10-01&end=2026-10-30&mode=paper", headers=HTMX)

    assert response.status_code == 200
    assert "yesterday or earlier" in response.text
    assert ".csv?" not in response.text


def test_a_refusal_is_a_200_panel_with_its_message_and_no_links(redis):
    def refuse(journal_range):
        raise JournalRefusedError("fx_missing", "No ECB rate for 2026-10-29 yet. The alerter fetches it hourly.")

    with _client(redis, read_summary=refuse) as client:
        response = client.get(CHECK, headers=HTMX)

    assert response.status_code == 200
    assert "No ECB rate for 2026-10-29 yet." in response.text
    assert ".csv?" not in response.text


def test_postgres_down_is_a_200_error_panel(redis):
    def down(journal_range):
        raise OperationalError("select", {}, Exception("down"))

    with _client(redis, read_summary=down) as client:
        response = client.get(CHECK, headers=HTMX)

    assert response.status_code == 200
    assert "Check failed." in response.text
    assert "cannot read Postgres" in response.text


def test_an_htmx_check_without_a_session_gets_hx_redirect(redis):
    with TestClient(make_app(redis, Clock(NOW)), follow_redirects=False) as client:
        htmx = client.get(CHECK, headers=HTMX)
        plain = client.get(CHECK)
        summary = client.get(SUMMARY)

    assert htmx.headers["hx-redirect"].startswith("/login?next=")
    assert "<form" not in htmx.text  # the login page is never swapped into the panel
    assert plain.status_code == summary.status_code == 303


def _summary_csv(text):
    return {(r["section"], r["item"], r["currency"]): r["value"] for r in csv.DictReader(io.StringIO(text, newline=""))}


def test_summary_csv_is_an_attachment_with_the_same_figures_as_the_panel(redis, caplog):
    caplog.set_level("INFO")
    summary = _summary(net="12.6")
    with _client(redis, read_summary=lambda r: summary) as client:
        response = client.get(SUMMARY)
        panel = client.get(CHECK, headers=HTMX).text

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="kfs-journal-summary-demo-paper-2026-10-01-2026-10-29.csv"'
    )
    assert response.text.startswith("section,item,currency,value\r\n")
    values = _summary_csv(response.text)
    assert values["range", "generated_at_utc", ""].startswith("2026-10-30T10:0")
    assert values["range", "git_commit", ""] == "unknown"
    assert values["check", "status", ""] == "mismatch"
    for key in (("totals", "net", "USD"), ("check", "difference", "USD"), ("balance_start", "KRAKEN-001", "EUR")):
        assert values[key].lstrip("-") in panel
    assert _codes(caplog) == [
        {
            "code": "journal_exported",
            "env": "demo",
            "mode": "paper",
            "start": "2026-10-01",
            "end": "2026-10-29",
            "file": "summary",
            "rows": 2,
        }
    ]


def _raising(error):
    def read(journal_range):
        raise error

    return read


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (JournalRefusedError("fx_missing", "Currency BTC has no EUR rate."), 409),
        (OperationalError("select", {}, Exception("down")), 503),
    ],
    ids=["refused", "postgres down"],
)
def test_summary_csv_refuses_as_plain_text(redis, error, status):
    with _client(redis, read_summary=_raising(error)) as client:
        response = client.get(SUMMARY)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/plain")
    assert "content-disposition" not in response.headers


def test_summary_csv_bad_range_is_422(redis):
    with _client(redis) as client:
        response = client.get("/trades/journal-summary.csv?start=2026-10-01&end=2026-10-29&mode=both")

    assert response.status_code == 422
    assert response.text == "The mode must be live or paper."
