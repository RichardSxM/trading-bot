"""covers: spec 0007 AC-6 (retention deletes whole backed up hours, oldest first, with their quarantine)."""

import json
import logging
import os
from decimal import Decimal

import pytest

from recorder.compactor.main import CompactorSettings, run_retention
from recorder.compactor.store import DELTAS_TABLE, INSTRUMENTS_TABLE, TRADES_TABLE, CatalogStore
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS

H0 = 1_759_708_800 * SECOND_NS  # 2025-10-06 00:00 UTC
NOW = H0 + 10 * HOUR_NS
INSTRUMENTS = ("PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN")
SIZE = 1_000


def _codes(caplog):
    return [(r.levelname, r.fields["code"]) for r in caplog.records if hasattr(r, "fields")]


def _write(path, mtime_ns, size=SIZE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _hour(store, hour, *, quarantine=False):
    """Every table and instrument of one hour, written 10 minutes after the hour ended."""
    mtime = hour + HOUR_NS + 600 * SECOND_NS
    paths = [
        _write(store.data_file(table, instrument, hour), mtime)
        for table in (DELTAS_TABLE, TRADES_TABLE, INSTRUMENTS_TABLE)
        for instrument in INSTRUMENTS
    ]
    if quarantine:
        paths.append(_write(store.quarantine_file(DELTAS_TABLE, INSTRUMENTS[0], hour), mtime))
    return paths


def _state(tmp_path, cutoff, snapshot_id="f0603109"):
    path = tmp_path / "state" / "catalog_backup.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"snapshot_id": snapshot_id, "cutoff": cutoff}))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = CatalogStore(tmp_path / "catalog")
    monkeypatch.setattr(store, "free_bytes", lambda: 100 * SIZE)
    return store


def _run(store, state_path, *, cap_bytes, floor_bytes=0):
    run_retention(store, state_path=state_path, cap_bytes=cap_bytes, floor_bytes=floor_bytes, now_ns=NOW)


def test_whole_hours_go_oldest_first_with_their_quarantine(store, tmp_path, caplog):
    first = _hour(store, H0, quarantine=True)
    second = _hour(store, H0 + HOUR_NS)
    third = _hour(store, H0 + 2 * HOUR_NS)
    state = _state(tmp_path, "2025-10-06T09:00:00Z")
    with caplog.at_level(logging.INFO):
        # Each hour holds 6 data files; the cap keeps one hour and a half.
        _run(store, state, cap_bytes=9 * SIZE)
    assert not any(p.exists() for p in first + second)
    assert all(p.exists() for p in third)
    assert _codes(caplog) == [("INFO", "catalog_retention_deleted")] * 2
    assert caplog.records[0].fields == {"code": "catalog_retention_deleted", "hour_ns": H0, "bytes": 7 * SIZE}


def test_hours_after_the_cutoff_stay_and_retention_is_blocked(store, tmp_path, caplog):
    first = _hour(store, H0)
    second = _hour(store, H0 + HOUR_NS)
    # Hour 0 was last written at 01:10, hour 1 at 02:10.
    state = _state(tmp_path, "2025-10-06T01:30:00Z")
    with caplog.at_level(logging.INFO):
        _run(store, state, cap_bytes=0)
    assert not any(p.exists() for p in first)
    assert all(p.exists() for p in second)
    assert _codes(caplog) == [("INFO", "catalog_retention_deleted"), ("ERROR", "catalog_retention_blocked")]
    assert caplog.records[1].fields["bytes_needed"] == 6 * SIZE


def test_the_free_floor_deletes_with_the_cap_far_away(store, tmp_path):
    first = _hour(store, H0)
    second = _hour(store, H0 + HOUR_NS)
    _run(store, _state(tmp_path, "2025-10-06T09:00:00Z"), cap_bytes=10**12, floor_bytes=105 * SIZE)
    assert not any(p.exists() for p in first)
    assert all(p.exists() for p in second)


def test_no_state_file_deletes_nothing(store, tmp_path, caplog):
    paths = _hour(store, H0)
    with caplog.at_level(logging.INFO):
        _run(store, tmp_path / "missing.json", cap_bytes=0)
    assert all(p.exists() for p in paths)
    assert _codes(caplog) == [("ERROR", "catalog_retention_blocked")]
    assert caplog.records[0].fields["has_backup_state"] is False


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"snapshot_id": "", "cutoff": "2025-10-06T09:00:00Z"}),
        json.dumps({"snapshot_id": "a", "cutoff": "2025-10-06T11:00:00Z"}),  # after now
    ],
)
def test_an_invalid_state_file_deletes_nothing_and_says_so(store, tmp_path, caplog, text):
    paths = _hour(store, H0)
    state = tmp_path / "catalog_backup.json"
    state.write_text(text)
    with caplog.at_level(logging.INFO):
        _run(store, state, cap_bytes=0)
    assert all(p.exists() for p in paths)
    assert _codes(caplog) == [("ERROR", "backup_state_invalid"), ("ERROR", "catalog_retention_blocked")]


def test_nothing_happens_under_both_limits(store, tmp_path, caplog):
    paths = _hour(store, H0)
    with caplog.at_level(logging.INFO):
        _run(store, _state(tmp_path, "2025-10-06T09:00:00Z"), cap_bytes=6 * SIZE)
    assert all(p.exists() for p in paths)
    assert _codes(caplog) == []


def test_the_inventory_ignores_files_that_are_not_one_whole_hour(store):
    _hour(store, H0)
    stray = store.root / "data" / DELTAS_TABLE / "PF_XBTUSD.KRAKEN" / "notes.parquet"
    _write(stray, H0)
    hours, catalog_bytes = store.hour_inventory()
    assert [h.hour_ns for h in hours] == [H0]
    assert catalog_bytes == 6 * SIZE


def test_settings_default_to_the_spec(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    settings = CompactorSettings()
    floors = (settings.catalog_cap_gb, settings.catalog_free_floor_gb, settings.catalog_hard_floor_gb)
    assert floors == (Decimal(30), Decimal(8), Decimal(2))
    assert str(settings.backup_state_path) == "/state/catalog_backup.json"
