"""Pure condition checks the alerter feeds the engine (spec 0013, *Value sourcing*)."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Final

from trading_core.alerts.model import CheckResult
from trading_core.alerts.rules import DISK_LOW, ENTRY_BLOCK, TRADER_DOWN
from trading_core.strategy.blocks import BlockReason

_GB: Final = 1_000_000_000
_NS: Final = 1_000_000_000

ENTRY_BLOCK_REASONS: Final = frozenset(
    {
        BlockReason.STALE_FEED,
        BlockReason.RECORDING_UNHEALTHY,
        BlockReason.LOSS_UNKNOWN,
        BlockReason.STOP_NOT_ACCEPTED,
        BlockReason.EXIT_FAILED,
        BlockReason.RECORD_CONFLICT,
        BlockReason.RECORDING_FAILED,
        BlockReason.RECONCILING,
        BlockReason.EXCHANGE_UNKNOWN,
        BlockReason.ORDER_UNRESOLVED,
    }
)
"""The reasons a stuck block alerts on (AC-6; the three check reasons from spec 0023 AC-21).
``halted``, ``paused``, ``daily_loss_limit``,
``trade_cap``, and ``loss_cooldown`` never do: you set them, they raised their own event, or
they clear on a known clock."""


def reachable(rule_id: str, ok: bool, now: datetime) -> CheckResult:
    """``postgres_unreachable`` or ``redis_unreachable`` from the alerter's own check (AC-4)."""
    return CheckResult(rule_id, ok=ok, at=now)


def heartbeat_check(ts_ns: int | None, now: datetime, threshold_s: int) -> CheckResult:
    """``trader_down`` from the heartbeat's ``ts_ns`` (AC-1).

    ``None`` (no key, or a value that is not a valid envelope) counts as older than the threshold.
    A heartbeat stamped after ``now`` (clock skew) counts as fresh.
    """
    if ts_ns is None:
        return CheckResult(TRADER_DOWN.id, ok=False, at=now, fields={"missing": True})
    now_ns = int(now.timestamp()) * 1_000_000_000 + now.microsecond * 1_000
    age_s = max(0, (now_ns - ts_ns) // 1_000_000_000)
    return CheckResult(TRADER_DOWN.id, ok=age_s <= threshold_s, at=now, fields={"age_s": age_s})


def disk_check(free_bytes: int, floor_gb: Decimal, now: datetime) -> CheckResult:
    """``disk_low`` from the host root filesystem's free bytes (AC-4).

    ``free_gb`` is text with one decimal, rounded down, so a value just under the floor never
    reads as the floor itself.
    """
    free_gb = (Decimal(free_bytes) / _GB).quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    fields = {"free_gb": str(free_gb), "floor_gb": str(floor_gb)}
    return CheckResult(DISK_LOW.id, ok=free_bytes >= floor_gb * _GB, at=now, fields=fields)


def entry_block_checks(blocked: Iterable[str], since_ns: Mapping[str, int], now: datetime) -> list[CheckResult]:
    """``entry_block`` for every alerting reason (AC-6): failing while it is in ``blocked``, else passing.

    ``since_ns`` is the trader's ``entries_blocked_since`` (reason to UTC ns); a reason missing
    from it (an older trader) counts from ``now``. Reasons outside ``ENTRY_BLOCK_REASONS`` and
    unknown ones are ignored.
    """
    active = set(blocked)
    results = []
    for reason in sorted(ENTRY_BLOCK_REASONS):
        if reason not in active:
            results.append(CheckResult(ENTRY_BLOCK.id, ok=True, at=now, subject=reason))
            continue
        ns = since_ns.get(reason)
        since = _from_ns(ns) if isinstance(ns, int) and ns > 0 else None
        results.append(
            CheckResult(ENTRY_BLOCK.id, ok=False, at=now, subject=reason, fields={"reason": reason}, since=since)
        )
    return results


def _from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns // _NS, UTC).replace(microsecond=ns % _NS // 1_000)
