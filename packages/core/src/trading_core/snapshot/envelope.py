"""The envelope around every Redis snapshot value (spec 0004).

A snapshot lives at ``{env}:snapshot:<name>`` as JSON ``{"schema_version": 1, "ts_ns": int,
"data": object}``. The writer stamps ``ts_ns`` so a reader can show how old the value is.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from trading_core.naming import key_prefix
from trading_core.settings import TradingEnv

SCHEMA_VERSION: Final = 1


class SnapshotFormatError(ValueError):
    """A snapshot value that is not valid JSON, lacks a field, has a wrong type, or an unknown version."""


@dataclass(frozen=True)
class Snapshot:
    """A decoded snapshot: when it was written (UTC nanoseconds) and its data."""

    schema_version: int
    ts_ns: int
    data: Mapping[str, object]


def snapshot_key(env: TradingEnv, name: str) -> str:
    """The Redis key of one snapshot, e.g. ``demo:snapshot:heartbeat``."""
    if not name or ":" in name:
        raise ValueError(f"snapshot name {name!r} must be non empty and contain no ':'")
    return f"{key_prefix(env, 'snapshot')}{name}"


def encode(ts_ns: int, data: Mapping[str, object]) -> str:
    """The JSON value for a snapshot written at ``ts_ns`` (UTC nanoseconds)."""
    return json.dumps({"schema_version": SCHEMA_VERSION, "ts_ns": ts_ns, "data": dict(data)}, separators=(",", ":"))


def decode(raw: str | bytes) -> Snapshot:
    """Parse a snapshot value; raises ``SnapshotFormatError`` for anything but a version 1 envelope."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SnapshotFormatError(f"snapshot is not JSON: {exc}") from None
    if not isinstance(value, dict):
        raise SnapshotFormatError("snapshot is not a JSON object")
    missing = [name for name in ("schema_version", "ts_ns", "data") if name not in value]
    if missing:
        raise SnapshotFormatError(f"snapshot lacks {missing}")
    version, ts_ns, data = value["schema_version"], value["ts_ns"], value["data"]
    # bool is an int subclass; a true or false here is a malformed value, not a number.
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise SnapshotFormatError(f"unknown snapshot schema_version {version!r}")
    if not isinstance(ts_ns, int) or isinstance(ts_ns, bool):
        raise SnapshotFormatError("snapshot ts_ns is not an integer")
    if not isinstance(data, dict):
        raise SnapshotFormatError("snapshot data is not an object")
    return Snapshot(schema_version=version, ts_ns=ts_ns, data=data)
