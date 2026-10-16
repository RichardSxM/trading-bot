"""covers: spec 0013 API surface, the alert command and result formats (AC-12 mute lengths, AC-16 the
badge's ``until``)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from trading_core.control.alerts import (
    AlertResult,
    alert_command,
    alerts_results_stream,
    alerts_stream,
    command_from_fields,
    command_to_fields,
    result_from_fields,
    result_to_fields,
)
from trading_core.control.messages import ControlFormatError
from trading_core.recording.enums import AlertCmd, ControlOutcome, ControlSource

NOW = datetime(2026, 10, 16, 14, 3, 7, 250_000, tzinfo=UTC)


def test_stream_names_are_env_scoped_and_apart_from_the_traders():
    assert alerts_stream("demo") == "demo:control:alerts"
    assert alerts_results_stream("live") == "live:control:alerts:results"


def test_a_mute_round_trips_with_its_length():
    msg = alert_command(AlertCmd.MUTE, NOW, 30, mute_s=3600)

    fields = command_to_fields(msg)

    assert fields["cmd"] == "alerts_mute"
    assert fields["mute_s"] == "3600"
    assert fields["expires_at"] == "2026-10-16T14:03:37.250000Z"
    assert command_from_fields(fields) == msg
    assert command_from_fields({k.encode(): v.encode() for k, v in fields.items()}) == msg


@pytest.mark.parametrize("cmd", [AlertCmd.UNMUTE, AlertCmd.TEST])
def test_unmute_and_test_carry_no_length(cmd):
    msg = alert_command(cmd, NOW, 30)

    assert "mute_s" not in command_to_fields(msg)
    assert command_from_fields(command_to_fields(msg)) == msg
    assert msg.source is ControlSource.DASHBOARD


@pytest.mark.parametrize("mute_s", [None, 999_999, 60])
def test_a_mute_of_another_length_is_refused(mute_s):
    with pytest.raises(ValueError, match="mute_s"):
        alert_command(AlertCmd.MUTE, NOW, 30, mute_s=mute_s)
    fields = command_to_fields(alert_command(AlertCmd.MUTE, NOW, 30, mute_s=3600))
    if mute_s is None:
        del fields["mute_s"]
    else:
        fields["mute_s"] = str(mute_s)
    with pytest.raises(ControlFormatError, match="mute_s"):
        command_from_fields(fields)


@pytest.mark.parametrize(
    "change", [{"cmd": "stop"}, {"mute_s": "x"}, {"schema_version": "2"}, {"id": "nope"}, {"issued_at": "today"}]
)
def test_a_bad_entry_is_a_format_error(change):
    fields = command_to_fields(alert_command(AlertCmd.MUTE, NOW, 30, mute_s=3600)) | change

    with pytest.raises(ControlFormatError):
        command_from_fields(fields)


def test_an_unmute_with_a_length_is_refused():
    fields = command_to_fields(alert_command(AlertCmd.UNMUTE, NOW, 30)) | {"mute_s": "3600"}

    with pytest.raises(ControlFormatError, match="no mute_s"):
        command_from_fields(fields)


def test_an_executed_mute_result_carries_until():
    result = AlertResult(uuid.uuid4(), ControlOutcome.EXECUTED, NOW, AlertCmd.MUTE, until=NOW + timedelta(hours=1))

    fields = result_to_fields(result)

    assert fields["until"] == "2026-10-16T15:03:07.250000Z"
    assert "reason" not in fields
    assert result_from_fields(fields) == result


def test_a_failed_result_carries_its_reason_and_no_until():
    result = AlertResult(uuid.uuid4(), ControlOutcome.FAILED, NOW, AlertCmd.TEST, reason="push_failed")

    fields = result_to_fields(result)

    assert "until" not in fields
    assert result_from_fields(fields) == result


def test_an_invalid_entry_result_names_no_cmd():
    result = AlertResult(uuid.uuid4(), ControlOutcome.FAILED, NOW, reason="invalid")

    assert "cmd" not in result_to_fields(result)
    assert result_from_fields(result_to_fields(result)) == result
