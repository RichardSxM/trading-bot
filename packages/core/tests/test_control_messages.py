"""covers: spec 0004 data model, the control command and result formats (AC-11, AC-12)."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_core.control.messages import (
    ControlFormatError,
    ControlMessage,
    ControlResult,
    control_stream,
    field,
    format_utc,
    from_fields,
    result_from_fields,
    results_stream,
    stop_command,
    to_fields,
)
from trading_core.recording.enums import ControlCmd, ControlOutcome

NOW = datetime(2026, 10, 2, 14, 3, 7, 250_000, tzinfo=UTC)
ID = "8b5e3c1a-6d2f-4c4e-9a7b-0f1e2d3c4b5a"


def test_stream_names_are_env_scoped():
    assert control_stream("demo") == "demo:control"
    assert results_stream("live") == "live:control:results"


def test_a_stop_command_has_a_uuid4_and_expires_after_the_ttl():
    msg = stop_command(NOW, 300)

    assert msg.id.version == 4
    assert msg.cmd is ControlCmd.STOP
    assert msg.issued_at == NOW
    assert msg.expires_at - msg.issued_at == timedelta(seconds=300)
    assert stop_command(NOW, 300).id != msg.id


def test_a_stop_command_converts_other_zones_to_utc():
    msg = stop_command(NOW.astimezone(timezone(timedelta(hours=2))), 60)
    assert msg.issued_at.utcoffset() == timedelta(0)
    assert msg.issued_at == NOW


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="naive"):
        stop_command(datetime(2026, 10, 2, 14, 3, 7), 300)  # noqa: DTZ001 the naive value under test
    with pytest.raises(ValueError, match="naive"):
        format_utc(datetime(2026, 10, 2))  # noqa: DTZ001


def test_fields_are_strings_with_z_timestamps_and_round_trip():
    msg = stop_command(NOW, 300)
    fields = to_fields(msg)

    assert fields == {
        "id": str(msg.id),
        "cmd": "stop",
        "issued_at": "2026-10-02T14:03:07.250000Z",
        "expires_at": "2026-10-02T14:08:07.250000Z",
        "schema_version": "1",
    }
    assert from_fields(fields) == msg
    assert from_fields({k.encode(): v.encode() for k, v in fields.items()}) == msg


def test_a_result_parses_from_bytes():
    raw = {b"id": ID.encode(), b"outcome": b"executed", b"outcome_at": b"2026-10-02T14:03:08Z", b"schema_version": b"1"}
    assert result_from_fields(raw) == ControlResult(
        uuid.UUID(ID), ControlOutcome.EXECUTED, datetime(2026, 10, 2, 14, 3, 8, tzinfo=UTC)
    )


GOOD_COMMAND = {
    "id": ID,
    "cmd": "stop",
    "issued_at": "2026-10-02T14:03:07Z",
    "expires_at": "2026-10-02T14:08:07Z",
    "schema_version": "1",
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": None}, "lacks"),
        ({"id": "nope"}, "not a UUID"),
        ({"cmd": "explode"}, "unknown control cmd"),
        ({"issued_at": "yesterday"}, "not ISO 8601"),
        ({"expires_at": "2026-10-02T14:08:07"}, "no timezone"),
        ({"schema_version": "2"}, "schema_version"),
    ],
)
def test_a_bad_command_raises_a_format_error(change, message):
    fields = {k: v for k, v in {**GOOD_COMMAND, **change}.items() if v is not None}
    with pytest.raises(ControlFormatError, match=message):
        from_fields(fields)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"outcome": None}, "lacks"),
        ({"outcome": "exploded"}, "unknown control outcome"),
        ({"outcome_at": "soon"}, "not ISO 8601"),
        ({"schema_version": "0"}, "schema_version"),
    ],
)
def test_a_bad_result_raises_a_format_error(change, message):
    good = {"id": ID, "outcome": "failed", "outcome_at": "2026-10-02T14:03:08Z", "schema_version": "1"}
    fields = {k: v for k, v in {**good, **change}.items() if v is not None}
    with pytest.raises(ControlFormatError, match=message):
        result_from_fields(fields)


def test_field_reads_str_and_bytes_and_refuses_bad_utf8():
    assert field({"id": ID}, "id") == ID
    assert field({b"id": ID.encode()}, "id") == ID
    assert field({b"id": b"\xff"}, "id") is None
    assert field({}, "id") is None


def test_control_message_is_a_value():
    msg = ControlMessage(uuid.UUID(ID), ControlCmd.STOP, NOW, NOW)
    assert msg == ControlMessage(uuid.UUID(ID), ControlCmd.STOP, NOW, NOW)


# --- gaps found against spec 0004 (/test pass)


def test_a_result_written_with_another_offset_parses_as_utc():
    """covers: AC-12 (the dashboard compares outcome_at in UTC)"""
    result = result_from_fields(
        {"id": ID, "outcome": "executed", "outcome_at": "2026-10-02T16:03:07+02:00", "schema_version": "1"}
    )
    assert result.outcome_at == datetime(2026, 10, 2, 14, 3, 7, tzinfo=UTC)
    assert result.outcome_at.utcoffset() == timedelta(0)


def test_a_key_that_is_not_utf8_is_skipped_not_a_crash():
    """covers: AC-12 (an entry that does not parse is unknown, never an exception)"""
    assert field({b"\xff\xfe": b"x", b"id": ID.encode()}, "id") == ID
    with pytest.raises(ControlFormatError):
        from_fields({b"\xff": b"x"})
