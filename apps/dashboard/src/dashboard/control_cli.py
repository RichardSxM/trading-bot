"""The deploy's view of the trader through Redis (spec 0005), run in a one off dashboard container.

    python -m dashboard.control_cli heartbeat

Runs with the dashboard's settings and its own Redis login (the ``dashboard`` ACL user), so it
needs no new grant. stdout carries exactly one JSON line, the result; anything else goes to
stderr. Exit codes: 0 fresh, 1 missing, stale, malformed, unreachable, or bad config.

Milestone 2 adds ``state``, ``pause``, ``unpause``, and ``wait-flat`` beside ``heartbeat``.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import ValidationError
from redis.exceptions import RedisError

from dashboard.settings import DashboardSettings
from dashboard.web.app import redis_client
from trading_core.secrets import REDIS_PASSWORD, SecretUnavailable, read_secret
from trading_core.snapshot.envelope import SnapshotFormatError, decode, snapshot_key

# A snapshot written more than this long ago means the writer is not running (spec 0005 AC-8).
MAX_AGE_S: Final = 5.0


class SnapshotReader(Protocol):
    """The one Redis call the CLI needs (redis.asyncio.Redis satisfies it)."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...


@dataclass(frozen=True)
class Result:
    """What one subcommand prints (``line``, one JSON object) and its exit code."""

    code: int
    line: dict[str, object]


def snapshot_result(raw: str | bytes | None, key: str, now_ns: int, max_age_s: float = MAX_AGE_S) -> Result:
    """Judge one raw snapshot value: fresh gives 0 with its data; missing, malformed, or stale gives 1."""
    if raw is None:
        return Result(1, {"key": key, "status": "missing"})
    try:
        snapshot = decode(raw)
    except SnapshotFormatError as exc:
        return Result(1, {"key": key, "status": "malformed", "error": str(exc)})
    age_s = (now_ns - snapshot.ts_ns) / 1e9
    # A snapshot slightly from the future (writer clock ahead) is fresh, not stale.
    status = "fresh" if age_s <= max_age_s else "stale"
    return Result(
        0 if status == "fresh" else 1,
        {"key": key, "status": status, "ts_ns": snapshot.ts_ns, "age_s": round(age_s, 3), "data": dict(snapshot.data)},
    )


async def read_snapshot(
    client: SnapshotReader, key: str, clock_ns: Callable[[], int] = time.time_ns, max_age_s: float = MAX_AGE_S
) -> Result:
    """Read ``key`` and judge it; an unreachable Redis gives 1 with the error's type."""
    try:
        raw = await client.get(key)
    except (RedisError, OSError) as exc:
        return Result(1, {"key": key, "status": "unreachable", "error": type(exc).__name__})
    return snapshot_result(raw, key, clock_ns(), max_age_s)


async def _heartbeat(settings: DashboardSettings) -> Result:
    client = redis_client(settings, read_secret(REDIS_PASSWORD, settings.secrets_dir))
    try:
        return await read_snapshot(client, snapshot_key(settings.trading_env, "heartbeat"))
    finally:
        await client.aclose()


def _fail(message: str, **fields: object) -> int:
    print(json.dumps({"status": "error", "error": message, **fields}), file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the subcommand, print its JSON line, and return its exit code."""
    parser = argparse.ArgumentParser(prog="python -m dashboard.control_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("heartbeat", help="print the trader heartbeat; exit 1 if missing or older than 5 s")
    args = parser.parse_args(argv)

    try:
        settings = DashboardSettings()
    except ValidationError as exc:
        return _fail("invalid configuration", errors=exc.errors(include_url=False, include_input=False))
    try:
        if args.command == "heartbeat":
            result = asyncio.run(_heartbeat(settings))
        else:  # argparse refuses anything else; kept so a new subcommand cannot fall through silently
            return _fail(f"unknown command {args.command}")
    except SecretUnavailable as exc:
        return _fail("secret unavailable", secret=exc.name, reason=exc.reason)
    print(json.dumps(result.line, separators=(",", ":")))
    return result.code


if __name__ == "__main__":
    sys.exit(main())
