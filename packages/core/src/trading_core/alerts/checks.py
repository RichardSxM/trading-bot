"""Pure condition checks the alerter feeds the engine (spec 0013, *Value sourcing*)."""

from datetime import datetime

from trading_core.alerts.model import CheckResult
from trading_core.alerts.rules import TRADER_DOWN


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
