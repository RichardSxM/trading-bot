"""covers: spec 0017 AC-1 (the audit's range, holdout refusal, missing day exit, report files, no engine) and AC-2
(byte identical JSON apart from ``generated_at``)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from nautilus_trader.core import nautilus_pyo3 as p3

from lab.audit import AuditRangeError, audit_days, run_audit
from lab.main import main

from .conftest import F_LAST, Row, lab_settings, snapshot, write_hour

BID, ASK = p3.OrderSide.BUY, p3.OrderSide.SELL
HOLDOUT = date(2025, 10, 19)
K = Decimal("1.5")
# 2022-04-01 00:00 UTC.
DAY0 = 1_648_771_200_000_000_000
SECOND = 1_000_000_000


def test_the_default_range_is_every_development_first_of_the_month():
    days = audit_days(None, None, HOLDOUT)

    assert (days[0], days[-1], len(days)) == (date(2022, 4, 1), date(2025, 10, 1), 43)


def test_a_range_reaching_the_holdout_is_refused():
    with pytest.raises(AuditRangeError, match="never reads holdout days"):
        audit_days(None, HOLDOUT, HOLDOUT)
    with pytest.raises(AuditRangeError) as problems:
        audit_days(date(2022, 1, 1), date(2021, 12, 1), HOLDOUT)
    assert len(problems.value.problems) == 2
    with pytest.raises(AuditRangeError, match="no 1st of the month"):
        audit_days(date(2022, 4, 2), date(2022, 4, 30), HOLDOUT)


def test_lab_audit_exits_64_for_the_holdout_and_78_when_live(monkeypatch, capsys):
    with pytest.raises(SystemExit) as done:
        main(["audit", "--to", "2025-10-19"])
    assert done.value.code == 64
    assert "never reads holdout days" in capsys.readouterr().err

    monkeypatch.setenv("TRADING_ENV", "live")
    with pytest.raises(SystemExit) as done:
        main(["audit"])
    assert done.value.code == 78


def _manifest(root, day: date) -> None:
    path = root / "manifests" / f"{day.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    streams: list[dict[str, object]] = [
        {"type": kind, "instrument_id": "PF_XBTUSD.KRAKEN", "incomplete_hours": []}
        for kind in ("order_book_deltas", "trade_tick")
    ]
    path.write_text(json.dumps({"date": day.isoformat(), "gaps": [], "streams": streams}), encoding="utf-8")


def test_a_day_with_no_manifest_exits_2_before_reading_anything(tmp_path, capsys):
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    _manifest(tmp_path / "catalog" / "tardis", date(2022, 4, 1))

    code = run_audit(settings, [date(2022, 4, 1), date(2022, 5, 1)], K, datetime(2026, 10, 21, tzinfo=UTC))

    assert code == 2
    err = capsys.readouterr().err
    assert "no manifest for 2022-05-01" in err
    assert "lab import-tardis 2022-05-01 2022-05-01" in err
    assert not (tmp_path / "reports").exists()


def test_the_audit_writes_its_reports_and_reruns_identically(tmp_path):
    root = tmp_path / "catalog" / "tardis"
    _manifest(root, date(2022, 4, 1))
    book = [(BID, "40000", "1"), (ASK, "40001", "2")]
    rows = [*snapshot(DAY0, book), Row(p3.BookAction.UPDATE, ASK, "40001", "3", DAY0 + 90 * SECOND, F_LAST)]
    write_hour(root, DAY0, rows, [("40001", "0.5", DAY0 + 30 * SECOND)])
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")

    first = run_audit(settings, [date(2022, 4, 1)], K, datetime(2026, 10, 21, 9, tzinfo=UTC))
    report = tmp_path / "reports" / "audit-20261021" / "audit.json"
    once = json.loads(report.read_text(encoding="utf-8"))
    second = run_audit(settings, [date(2022, 4, 1)], K, datetime(2026, 10, 21, 10, tzinfo=UTC))
    again = json.loads(report.read_text(encoding="utf-8"))

    assert (first, second) == (0, 0)
    assert once.pop("generated_at") != again.pop("generated_at")
    assert once == again
    day = once["per_day"]["2022-04-01"]
    assert (day["clean_hours"], day["dropped_hours"]) == (1, {"missing": 23})
    assert once["pooled"]["seconds_sampled"] == 3600
    assert once["pooled"]["trades_per_minute_p50"] == 0
    assert once["pooled"]["aggressive_buy_share"] == "1.0000"
    assert once["reachable"] == {"maker_entry": False, "taker_entry": False}
    assert (tmp_path / "reports" / "audit-20261021" / "audit.md").is_file()
