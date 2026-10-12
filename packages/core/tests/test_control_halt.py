"""covers: spec 0010 AC-5, the halt key: its name, its value, and what is refused."""

import uuid
from datetime import UTC, datetime

import pytest

from trading_core.control.halt import HaltFormatError, HaltReason, HaltSource, HaltState, decode, encode, halt_key

NOW = datetime(2026, 10, 12, 9, 30, 1, 500_000, tzinfo=UTC)
STATE = HaltState(
    HaltReason.DAILY_LOSS_LIMIT, HaltSource.TRADER, NOW, uuid.UUID("0199d7a4-6d2f-7c4e-9a7b-0f1e2d3c4b5a")
)


def test_the_key_is_env_scoped():
    assert halt_key("demo") == "demo:trader:halt"
    assert halt_key("live") == "live:trader:halt"


def test_encode_round_trips_from_text_and_bytes():
    raw = encode(STATE)

    assert '"reason":"daily_loss_limit"' in raw
    assert '"source":"trader"' in raw
    assert '"halted_at":"2026-10-12T09:30:01.500000Z"' in raw
    assert decode(raw) == STATE
    assert decode(raw.encode()) == STATE


def test_decode_converts_other_zones_to_utc():
    raw = encode(STATE).replace("09:30:01.500000Z", "11:30:01.500000+02:00")

    assert decode(raw).halted_at == NOW


@pytest.mark.parametrize(
    "raw",
    [
        "notjson",
        "[]",
        "{}",
        encode(STATE).replace("daily_loss_limit", "bored"),
        encode(STATE).replace("daily_loss_limit", "unknown"),  # never written, only held in memory
        encode(STATE).replace('"trader"', '"someone"'),
        encode(STATE).replace("2026-10-12T09:30:01.500000Z", "2026-10-12T09:30:01"),
        encode(STATE).replace("0199d7a4", "nothex!"),
        b"\xff\xfe",
    ],
)
def test_anything_encode_would_not_write_is_refused(raw):
    with pytest.raises(HaltFormatError):
        decode(raw)
