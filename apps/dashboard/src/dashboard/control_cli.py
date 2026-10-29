"""The deploy's view of and control over the trader through Redis (spec 0005), run in a one off
dashboard container.

    python -m dashboard.control_cli heartbeat
    python -m dashboard.control_cli state
    python -m dashboard.control_cli pause --source deploy --wait-result 30
    python -m dashboard.control_cli unpause --source deploy --wait-result 30
    python -m dashboard.control_cli wait-flat --timeout 15m
    python -m dashboard.control_cli stop --wait-result 30
    python -m dashboard.control_cli flatten --wait-result 30
    python -m dashboard.control_cli resume --wait-result 30
    python -m dashboard.control_cli recheck --wait-result 60

Runs with the dashboard's settings and its own Redis login (the ``dashboard`` ACL user), so it
needs no new grant. stdout carries exactly one JSON line, the result; anything else goes to
stderr. Exit codes:

- ``heartbeat``, ``state``: 0 fresh, 1 missing, stale, malformed, unreachable, or bad config.
- ``pause``, ``unpause``: 0 when the trader answers ``executed`` in time, 1 otherwise.
- ``wait-flat``: 0 after two fresh ``trader_state`` reads at least 2 s apart both show no open
  position and no open entry order, 2 on timeout, 3 when the pause is lifted, 1 on a missing or
  stale snapshot.
- ``stop``, ``flatten``, ``resume`` (spec 0010 AC-19, sent with ``source = cli``): 0 on ``executed``,
  1 on ``failed``, ``expired``, or ``duplicate`` (the reason is printed), 2 when Redis cannot be
  reached or no result came in time.
- ``recheck`` (spec 0023 AC-20, ``source = cli``): runs a reconciliation check now; the same codes, with
  the check's ``outcome`` and ``reconciliation_id`` printed under ``details``.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from pydantic import ValidationError
from redis.exceptions import RedisError
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT

from dashboard.settings import DashboardSettings
from dashboard.web.app import redis_client
from dashboard.web.control_routes import STREAM_MAXLEN
from trading_core.control.messages import (
    ControlFormatError,
    ControlMessage,
    ControlResult,
    command,
    control_stream,
    result_from_fields,
    results_stream,
    to_fields,
)
from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource
from trading_core.secrets import REDIS_PASSWORD, SecretUnavailable, read_secret
from trading_core.settings import TradingEnv
from trading_core.snapshot.envelope import SnapshotFormatError, decode, snapshot_key

# A snapshot written more than this long ago means the writer is not running (spec 0005 AC-8).
MAX_AGE_S: Final = 5.0
# A deploy's pause expires this soon when the trader never reads it (spec 0005 deploy sequence).
COMMAND_TTL_S: Final = 60
POLL_S: Final = 0.25
FLAT_POLL_S: Final = 1.0
# Two flat reads this far apart (by their ts_ns) count as flat (AC-8).
FLAT_APART_NS: Final = 2_000_000_000
RESULTS_SCAN: Final = 200
FINAL_OUTCOMES: Final = frozenset(
    {ControlOutcome.EXECUTED, ControlOutcome.FAILED, ControlOutcome.EXPIRED, ControlOutcome.DUPLICATE}
)

EXIT_TIMEOUT: Final = 2
EXIT_PAUSE_LIFTED: Final = 3


class SnapshotReader(Protocol):
    """The one Redis call the snapshot reads need (redis.asyncio.Redis satisfies it)."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...


class ControlClient(SnapshotReader, Protocol):
    """The Redis calls ``pause`` and ``unpause`` need (redis.asyncio.Redis satisfies it)."""

    def xadd(
        self,
        name: KeyT,
        fields: dict[FieldT, EncodableT],
        id: StreamIdT = "*",
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> Awaitable[object]: ...

    def xrevrange(self, name: KeyT, max: str = "+", min: str = "-", count: int | None = None) -> Awaitable[object]: ...


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


async def send_and_wait(
    client: ControlClient,
    msg: ControlMessage,
    env: TradingEnv,
    wait_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    poll_s: float = POLL_S,
) -> Result:
    """Append ``msg`` to the control stream and wait up to ``wait_s`` for its final result.

    Exit 0 only on ``executed``; ``failed``, ``expired``, ``duplicate``, no answer in time, or an
    unreachable Redis give 1.
    """
    base: dict[str, object] = {"id": str(msg.id), "cmd": msg.cmd.value, "source": msg.source.value}
    fields: dict[FieldT, EncodableT] = {name: value for name, value in to_fields(msg).items()}
    try:
        await client.xadd(control_stream(env), fields, maxlen=STREAM_MAXLEN)
    except (RedisError, OSError) as exc:
        return Result(1, base | {"status": "not_sent", "error": type(exc).__name__})
    deadline = monotonic() + wait_s
    while True:
        error: str | None
        try:
            outcome = await _final_outcome(client, results_stream(env), msg)
        except (RedisError, OSError) as exc:
            outcome = None
            error = type(exc).__name__
        else:
            error = None
        if outcome is not None:
            line = (
                base
                | {"status": outcome.outcome.value}
                | ({"reason": outcome.reason} if outcome.reason else {})
                | ({"details": dict(outcome.details)} if outcome.details else {})
            )
            return Result(0 if outcome.outcome is ControlOutcome.EXECUTED else 1, line)
        if monotonic() >= deadline:
            return Result(1, base | {"status": "no_result"} | ({"error": error} if error else {}))
        await asyncio.sleep(poll_s)


async def _final_outcome(client: ControlClient, stream: str, msg: ControlMessage) -> ControlResult | None:
    entries = await client.xrevrange(stream, count=RESULTS_SCAN)
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, list | tuple) or len(entry) != 2 or not isinstance(entry[1], Mapping):
            continue
        try:
            result = result_from_fields(entry[1])
        except ControlFormatError:
            continue
        if result.id == msg.id and result.outcome in FINAL_OUTCOMES:
            return result
    return None


def _flat(data: Mapping[str, object]) -> bool:
    return data.get("open_positions") == 0 and data.get("open_entry_orders") == 0


async def wait_flat(
    client: SnapshotReader,
    key: str,
    timeout_s: float,
    clock_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
    poll_s: float = FLAT_POLL_S,
) -> Result:
    """Poll ``trader_state`` until two fresh reads at least 2 s apart are flat (AC-8).

    Ends early with 3 when ``paused`` turns false (you pressed Resume) and with 1 on a missing,
    stale, malformed, or unreadable snapshot; 2 when ``timeout_s`` passes first.
    """
    started = monotonic()
    first_flat_ns: int | None = None
    while True:
        read = await read_snapshot(client, key, clock_ns)
        waited = round(monotonic() - started, 3)
        if read.code != 0:
            return Result(1, read.line | {"waited_s": waited})
        data, ts_ns = read.line["data"], read.line["ts_ns"]
        if not isinstance(data, Mapping) or not isinstance(ts_ns, int):  # fresh reads always have both
            return Result(1, {"key": key, "status": "malformed", "waited_s": waited})
        if data.get("paused") is not True:
            return Result(EXIT_PAUSE_LIFTED, {"key": key, "status": "pause_lifted", "waited_s": waited})
        if not _flat(data):
            first_flat_ns = None
        elif first_flat_ns is None:
            first_flat_ns = ts_ns
        elif ts_ns - first_flat_ns >= FLAT_APART_NS:
            return Result(0, {"key": key, "status": "flat", "waited_s": waited})
        if monotonic() - started >= timeout_s:
            return Result(
                EXIT_TIMEOUT,
                {
                    "key": key,
                    "status": "not_flat",
                    "waited_s": waited,
                    "open_positions": data.get("open_positions"),
                    "open_entry_orders": data.get("open_entry_orders"),
                },
            )
        await asyncio.sleep(poll_s)


def parse_duration(text: str) -> float:
    """Seconds in ``90``, ``90s``, ``15m``, or ``1h``; raises ``ValueError`` otherwise."""
    match = re.fullmatch(r"(\d+)([smh]?)", text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (use 90, 90s, 15m, or 1h)")
    return float(int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)])


async def _with_client(settings: DashboardSettings, run: Callable[[ControlClient], Awaitable[Result]]) -> Result:
    client = redis_client(settings, read_secret(REDIS_PASSWORD, settings.secrets_dir))
    try:
        return await run(client)
    finally:
        await client.aclose()


# The kill switch commands (spec 0010 AC-19) and their exit code when the command never got an answer.
KILL_SWITCH_CMDS: Final = {"stop": ControlCmd.STOP, "flatten": ControlCmd.FLATTEN, "resume": ControlCmd.RESUME}
# Sent as cli with the same exit codes: the kill switch commands plus the reconciliation recheck (spec 0023 AC-20).
CLI_CMDS: Final = KILL_SWITCH_CMDS | {"recheck": ControlCmd.RECHECK}
EXIT_NO_ANSWER: Final = 2


async def send_kill_switch(client: ControlClient, msg: ControlMessage, env: TradingEnv, wait_s: float) -> Result:
    """``send_and_wait`` for ``stop``, ``flatten``, ``resume``: no answer (not sent, none in time) exits 2."""
    result = await send_and_wait(client, msg, env, wait_s)
    if result.line.get("status") in ("not_sent", "no_result"):
        return Result(EXIT_NO_ANSWER, result.line)
    return result


def _run(args: argparse.Namespace, settings: DashboardSettings) -> Result:
    env = settings.trading_env
    if args.command in ("heartbeat", "state"):
        key = snapshot_key(env, "heartbeat" if args.command == "heartbeat" else "trader_state")
        return asyncio.run(_with_client(settings, lambda client: read_snapshot(client, key)))
    if args.command in ("pause", "unpause"):
        cmd = ControlCmd.PAUSE if args.command == "pause" else ControlCmd.UNPAUSE
        msg = command(cmd, datetime.now(UTC), args.ttl, ControlSource(args.source))
        return asyncio.run(_with_client(settings, lambda client: send_and_wait(client, msg, env, args.wait_result)))
    if args.command in CLI_CMDS:
        msg = command(CLI_CMDS[args.command], datetime.now(UTC), args.ttl, ControlSource.CLI)
        return asyncio.run(_with_client(settings, lambda client: send_kill_switch(client, msg, env, args.wait_result)))
    key = snapshot_key(env, "trader_state")
    return asyncio.run(_with_client(settings, lambda client: wait_flat(client, key, args.timeout)))


def _fail(message: str, **fields: object) -> int:
    print(json.dumps({"status": "error", "error": message, **fields}), file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the subcommand, print its JSON line, and return its exit code."""
    parser = argparse.ArgumentParser(prog="python -m dashboard.control_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("heartbeat", help="print the trader heartbeat; exit 1 if missing or older than 5 s")
    sub.add_parser("state", help="print trader_state; exit 1 if missing or older than 5 s")
    for name in ("pause", "unpause"):
        cmd = sub.add_parser(name, help=f"send {name} and wait for the trader's answer; exit 0 on executed")
        cmd.add_argument("--source", choices=[s.value for s in ControlSource], default=ControlSource.DEPLOY.value)
        cmd.add_argument("--wait-result", type=float, default=30.0, metavar="SECONDS")
        cmd.add_argument("--ttl", type=int, default=COMMAND_TTL_S, metavar="SECONDS")
    for name in CLI_CMDS:
        cmd = sub.add_parser(name, help=f"send {name} as cli and wait for the trader's answer; exit 0 on executed")
        # A recheck may wait out two Kraken timeouts before it answers.
        cmd.add_argument("--wait-result", type=float, default=60.0 if name == "recheck" else 30.0, metavar="SECONDS")
        cmd.add_argument("--ttl", type=int, default=COMMAND_TTL_S, metavar="SECONDS")
    flat = sub.add_parser("wait-flat", help="wait until the paused trader holds no position and no entry order")
    flat.add_argument("--timeout", type=parse_duration, default=parse_duration("15m"), metavar="DURATION")
    args = parser.parse_args(argv)

    try:
        settings = DashboardSettings()
    except ValidationError as exc:
        return _fail("invalid configuration", errors=exc.errors(include_url=False, include_input=False))
    try:
        result = _run(args, settings)
    except SecretUnavailable as exc:
        return _fail("secret unavailable", secret=exc.name, reason=exc.reason)
    print(json.dumps(result.line, separators=(",", ":")))
    return result.code


if __name__ == "__main__":
    sys.exit(main())
