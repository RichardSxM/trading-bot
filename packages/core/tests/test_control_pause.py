"""covers: spec 0005 AC-6, the pause key and the trading state a pause maps to."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_core.control.pause import PauseFormatError, PauseState, decode, encode, pause_key, target_trading_state
from trading_core.recording.enums import ControlSource

NOW = datetime(2026, 10, 3, 9, 30, 1, 500_000, tzinfo=UTC)
STATE = PauseState(ControlSource.DEPLOY, NOW, uuid.UUID("8b5e3c1a-6d2f-4c4e-9a7b-0f1e2d3c4b5a"))


def test_the_key_is_env_scoped():
    assert pause_key("demo") == "demo:trader:pause"
    assert pause_key("live") == "live:trader:pause"


def test_encode_round_trips_from_text_and_bytes():
    raw = encode(STATE)

    assert '"source":"deploy"' in raw
    assert '"paused_at":"2026-10-03T09:30:01.500000Z"' in raw
    assert decode(raw) == STATE
    assert decode(raw.encode()) == STATE


def test_decode_converts_other_zones_to_utc():
    raw = encode(STATE).replace("09:30:01.500000Z", "11:30:01.500000+02:00")

    assert decode(raw).paused_at == NOW
    assert decode(raw).paused_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        b"\xff",
        "[]",
        '{"source":"deploy","paused_at":"2026-10-03T09:30:01Z"}',
        '{"source":"cron","paused_at":"2026-10-03T09:30:01Z","command_id":"8b5e3c1a-6d2f-4c4e-9a7b-0f1e2d3c4b5a"}',
        '{"source":"deploy","paused_at":"soon","command_id":"8b5e3c1a-6d2f-4c4e-9a7b-0f1e2d3c4b5a"}',
        '{"source":"deploy","paused_at":"2026-10-03T09:30:01","command_id":"8b5e3c1a-6d2f-4c4e-9a7b-0f1e2d3c4b5a"}',
        '{"source":"deploy","paused_at":"2026-10-03T09:30:01Z","command_id":"nope"}',
    ],
)
def test_decode_refuses_anything_encode_would_not_write(raw):
    with pytest.raises(PauseFormatError):
        decode(raw)


def test_a_paused_or_halted_trader_reduces_and_a_running_one_is_active():
    """covers: spec 0010 (a halt maps to REDUCING, never HALTED)"""
    assert target_trading_state(paused=True, halted=False) == "REDUCING"
    assert target_trading_state(paused=False, halted=True) == "REDUCING"
    assert target_trading_state(paused=True, halted=True) == "REDUCING"
    assert target_trading_state(paused=False, halted=False) == "ACTIVE"


def test_paused_at_from_other_zones_encodes_as_utc():
    other = PauseState(ControlSource.DASHBOARD, NOW.astimezone(timezone(timedelta(hours=-5))), STATE.command_id)

    assert decode(encode(other)).paused_at == NOW
