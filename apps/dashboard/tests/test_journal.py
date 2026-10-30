"""covers: spec 0024 AC-1 (the export form on Trades with its defaults), AC-2 (bad ranges get our own 422 text),
AC-3 (the CSV response's type and filename), AC-5 and AC-6 (a refusal is a 409 with its message), AC-12 (behind the
login, logged without amounts), AC-14 (a failure mid stream aborts the response)."""

from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from trading_core.journal.bounds import JournalRange
from trading_core.journal.export import JournalRefusedError

from .conftest import Clock, FakeExport, log_in, make_app

# Friday 30 October 2026, 10:00 UTC: yesterday in Amsterdam is the 29th.
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC).timestamp()
GOOD = "/trades/journal.csv?start=2026-10-01&end=2026-10-29&mode=paper"


@contextmanager
def _client(redis, open_export=None, env="demo", clock=None):
    clock = clock or Clock(NOW)
    app = make_app(redis, clock, env=env, open_export=open_export)
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
    assert 'action="/trades/journal.csv"' in page
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
