"""covers: spec 0023 AC-19, the Reconciliation page: reported checks newest first, 50 per page, each with its
findings and actions in plain words; 404 past the last page; a Postgres failure says so; the badge links here."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from dashboard.web.reconciliations import mismatch_text, reconciliation_cells
from trading_core.recording.queries import PAGE_SIZE, MismatchLine, ReconciliationLine

from .conftest import log_in, make_app

AT = datetime(2026, 10, 30, 0, 15, tzinfo=UTC)
FOUND = MismatchLine(
    kind="unknown_position",
    instrument_id="PF_XBTUSD.KRAKEN",
    client_order_id=None,
    exchange_side="long",
    exchange_qty=Decimal("0.0001"),
    local_side=None,
    local_qty=None,
    action="flattened",
    action_result="pending",
)


def _line(outcome="clean", mismatches=(), error=None, orders=0, positions=0):
    return ReconciliationLine(
        id=uuid7(),
        trigger="start",
        started_at=AT,
        finished_at=AT,
        outcome=outcome,
        exchange_orders=orders,
        exchange_positions=positions,
        error=error,
        mismatches=tuple(mismatches),
    )


def _get(redis, clock, read, path="/reconciliations"):
    with TestClient(make_app(redis, clock, read_reconciliations=read), follow_redirects=False) as client:
        assert log_in(client, clock).status_code == 303
        clock.now += 30
        return client.get(path)


def test_a_finding_reads_in_plain_words():
    assert mismatch_text(FOUND) == (
        "unknown position PF_XBTUSD · Kraken long 0.0001 · trader none · flattened, pending"
    )


def test_an_order_finding_names_the_order_and_leaves_out_empty_views():
    line = MismatchLine(
        "unknown_order", "PF_XBTUSD.KRAKEN", "drill-foreign-1", None, None, None, None, "cancelled", "ok"
    )

    assert mismatch_text(line) == "unknown order PF_XBTUSD · order drill-foreign-1 · cancelled, ok"


def test_a_failed_check_shows_its_error_and_blank_counts():
    cells = reconciliation_cells(_line("failed", error="exchange_unavailable", orders=None, positions=None))

    assert (cells[1], cells[3], cells[4]) == ("start", "—", "—")
    assert 'badge--bad">failed<' in str(cells[2])
    assert "failed: exchange unavailable" in str(cells[5])


def test_the_page_lists_checks_with_their_findings(redis, clock):
    asked = []

    def read(page):
        asked.append(page)
        return [_line("mismatch", [FOUND], orders=1, positions=1), _line()]

    response = _get(redis, clock, read)

    assert response.status_code == 200
    assert asked == [1]
    page = response.text
    assert "Kraken checks" in page
    assert 'badge--bad">mismatch<' in page
    assert 'badge--ok">clean<' in page
    assert "unknown position PF_XBTUSD · Kraken long 0.0001 · trader none · flattened, pending" in page
    assert 'aria-current="page"' in page.split('href="/system"')[1][:60]  # under System in the nav


def test_an_empty_first_page_says_checks_appear_here(redis, clock):
    response = _get(redis, clock, lambda page: [])

    assert response.status_code == 200
    assert "Checks appear here once the trader runs one." in response.text


def test_a_full_page_links_to_the_older_one(redis, clock):
    response = _get(redis, clock, lambda page: [_line()] * PAGE_SIZE, "/reconciliations?page=2")

    assert 'href="?page=3"' in response.text
    assert 'href="?page=1"' in response.text


def test_a_page_past_the_last_is_404(redis, clock):
    assert _get(redis, clock, lambda page: [], "/reconciliations?page=2").status_code == 404


def test_a_page_below_one_is_refused(redis, clock):
    assert _get(redis, clock, lambda page: [], "/reconciliations?page=0").status_code == 422


def test_a_postgres_failure_says_the_checks_are_unavailable(redis, clock):
    def broken(page):
        raise OperationalError("select", {}, Exception("down"))

    response = _get(redis, clock, broken)

    assert response.status_code == 200
    assert "Kraken checks unavailable." in response.text


def test_the_page_needs_a_session(client):
    response = client.get("/reconciliations")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Freconciliations"


def test_the_system_page_links_here(logged_in):
    assert 'href="/reconciliations"' in logged_in.get("/system").text
