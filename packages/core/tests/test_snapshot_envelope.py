"""covers: spec 0004 data model, the snapshot envelope the trader writes and the dashboard reads (AC-8, AC-9)."""

import json

import pytest

from trading_core.snapshot.envelope import Snapshot, SnapshotFormatError, decode, encode, snapshot_key


def test_key_is_env_scoped():
    assert snapshot_key("demo", "heartbeat") == "demo:snapshot:heartbeat"
    assert snapshot_key("live", "heartbeat") == "live:snapshot:heartbeat"


@pytest.mark.parametrize("name", ["", "a:b"])
def test_key_refuses_a_bad_name(name):
    with pytest.raises(ValueError, match="snapshot name"):
        snapshot_key("demo", name)


def test_round_trip():
    raw = encode(1_700_000_000_123_456_789, {"process": "trader", "data_connected": True})

    assert json.loads(raw) == {
        "schema_version": 1,
        "ts_ns": 1_700_000_000_123_456_789,
        "data": {"process": "trader", "data_connected": True},
    }
    assert decode(raw) == Snapshot(1, 1_700_000_000_123_456_789, {"process": "trader", "data_connected": True})
    assert decode(raw.encode()) == decode(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "not JSON"),
        (b"\xff", "not JSON"),
        ("[1]", "not a JSON object"),
        ('{"ts_ns": 1, "data": {}}', "lacks"),
        ('{"schema_version": 2, "ts_ns": 1, "data": {}}', "schema_version"),
        ('{"schema_version": true, "ts_ns": 1, "data": {}}', "schema_version"),
        ('{"schema_version": 1, "ts_ns": "1", "data": {}}', "ts_ns"),
        ('{"schema_version": 1, "ts_ns": 1.5, "data": {}}', "ts_ns"),
        ('{"schema_version": 1, "ts_ns": 1, "data": []}', "data"),
    ],
)
def test_decode_refuses_anything_but_a_version_1_envelope(raw, message):
    with pytest.raises(SnapshotFormatError, match=message):
        decode(raw)
