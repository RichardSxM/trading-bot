"""Reads the packaged ``trading_core/gate/thresholds.toml`` (spec 0015); the pure gate only parses bytes."""

from pathlib import Path

from trading_core.gate.thresholds import Thresholds, parse_thresholds

THRESHOLDS_PATH = Path(__file__).resolve().parent / "gate" / "thresholds.toml"


def packaged_thresholds_bytes() -> bytes:
    """The thresholds file as shipped in this image."""
    return THRESHOLDS_PATH.read_bytes()


def packaged_thresholds() -> Thresholds:
    """The thresholds file parsed; raises ``ThresholdsError`` when it is invalid."""
    return parse_thresholds(packaged_thresholds_bytes())
