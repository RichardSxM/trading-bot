"""Writes each book rebalance's sizing to Postgres (spec 0033 AC-6): ``book_plans`` and ``book_plan_coins``.

A task on the trader's event loop polls the book strategy's ``plan_snapshot`` on the snapshot interval. The
first time it sees a ``last_rebalance_at`` it has not handled, it builds that rebalance's rows once and keeps
them; it then writes and retries those same frozen rows (never a fresh read, since a newer plan may replace
the snapshot's before the write lands) in one transaction in a worker thread. This is its own write path,
not ``RecordingWriter``, because that writer fails closed and a sizing record must never block trading. A
failed write retries every tick (the unique key makes a retry safe) and logs ``book_plan_write_failed`` at
most once a minute. Nothing here lives in a strategy's code closure.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.book_plan_rows import BookPlanCoinRow, BookPlanReason, BookPlanRow
from trading_core.recording.enums import Env
from trading_core.recording.errors import RecordConflictError

log = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 1.0
WARN_EVERY_S: Final = 60.0


@dataclass(frozen=True)
class FrozenPlan:
    """One rebalance's rows, built once from the snapshot that first showed it."""

    row: BookPlanRow
    coins: tuple[BookPlanCoinRow, ...]


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _coin(item: Mapping[str, object]) -> BookPlanCoinRow | None:
    # A snapshot coin with no plan item has its plan fields None and gets no row.
    if item.get("eligible") is None or item.get("weight") is None or item.get("target_notional") is None:
        return None
    signal = item.get("signal")
    close_date = item.get("close_date")
    return BookPlanCoinRow(
        instrument_id=str(item["instrument_id"]),
        eligible=bool(item["eligible"]),
        signal=int(str(signal)) if signal is not None else 0,
        vol=_decimal(item.get("vol")),
        weight=Decimal(str(item["weight"])),
        target_notional=Decimal(str(item["target_notional"])),
        close_date=None if close_date is None else date.fromisoformat(str(close_date)),
    )


def book_plan_rows(snapshot: Mapping[str, object], env: Env, run_id: UUID, gross_cap: Decimal) -> FrozenPlan | None:
    """The ``book_plans`` row and coin rows for the snapshot's last rebalance, or None when the snapshot has no
    plan, no ``last_rebalance_at``, or no equity. A strategy that is not vol sized gives null vol fields.

    Raises ``ValueError`` for a reason ``book_plans`` does not know.
    """
    rebalanced_ns = snapshot.get("last_rebalance_at")
    equity, plan_day, n, gross = (snapshot.get(k) for k in ("equity", "plan_day", "n", "gross"))
    if rebalanced_ns is None or equity is None or plan_day is None or n is None or gross is None:
        return None
    raw_coins = snapshot.get("coins")
    coins = raw_coins if isinstance(raw_coins, Sequence) else ()
    row = BookPlanRow(
        env=env,
        run_id=run_id,
        plan_day=date.fromisoformat(str(plan_day)),
        reason=BookPlanReason(str(snapshot.get("last_reason"))),
        rebalanced_ns=int(str(rebalanced_ns)),
        equity=Decimal(str(equity)),
        n=int(str(n)),
        gross=Decimal(str(gross)),
        unit_vol=_decimal(snapshot.get("unit_vol")),
        book_gross=_decimal(snapshot.get("book_gross")),
        book_vol_target=_decimal(snapshot.get("book_vol_target")),
        rebalance_band=_decimal(snapshot.get("rebalance_band")),
        gross_cap=gross_cap,
    )
    built = (_coin(item) for item in coins if isinstance(item, Mapping))
    return FrozenPlan(row, tuple(coin for coin in built if coin is not None))


def write_plan(sessions: Callable[[], Session], plan: FrozenPlan) -> None:
    """Write one frozen plan in one transaction (blocking; run it in a worker thread).

    A ``RecordConflictError`` is reported through ``report_conflict`` in a fresh session, then raised.
    """
    try:
        with sessions() as session, session.begin():
            repository.record_book_plan(session, plan.row, plan.coins)
    except RecordConflictError as error:
        with sessions() as session:
            repository.report_conflict(session, error)
        raise


async def write_book_plans(
    read_snapshot: Callable[[], Mapping[str, object] | None],
    env: Env,
    run_id: UUID,
    gross_cap: Decimal,
    sessions: Callable[[], Session],
    interval_s: float = POLL_INTERVAL_S,
    monotonic: Callable[[], float] = time.monotonic,
    write: Callable[[Callable[[], Session], FrozenPlan], None] = write_plan,
) -> None:
    """Poll ``read_snapshot`` every ``interval_s`` and write each new rebalance's frozen rows, until cancelled.

    Plans wait in order; a failed write keeps its plan (and the ones after it) for the next tick. A conflict
    is reported and that plan dropped, since a retry would conflict again. Never raises out of a tick.
    """
    handled: set[int] = set()
    pending: list[FrozenPlan] = []
    last_warning: float | None = None
    while True:
        snapshot = read_snapshot()
        rebalanced = None if snapshot is None else snapshot.get("last_rebalance_at")
        if snapshot is not None and rebalanced is not None and int(str(rebalanced)) not in handled:
            try:
                frozen = book_plan_rows(snapshot, env, run_id, gross_cap)
            except ValueError, TypeError, KeyError:
                handled.add(int(str(rebalanced)))
                log.exception(
                    "book plan could not be built from the snapshot",
                    extra={"fields": {"code": "book_plan_invalid", "rebalanced_ns": str(rebalanced)}},
                )
            else:
                if frozen is not None:
                    handled.add(frozen.row.rebalanced_ns)
                    pending.append(frozen)
        while pending:
            plan = pending[0]
            try:
                await asyncio.to_thread(write, sessions, plan)
            except RecordConflictError:
                pending.pop(0)
                log.exception(
                    "book plan conflicts with a stored one, dropped",
                    extra={"fields": {"code": "book_plan_conflict", "rebalanced_ns": plan.row.rebalanced_ns}},
                )
            except Exception as exc:
                now = monotonic()
                if last_warning is None or now - last_warning >= WARN_EVERY_S:
                    last_warning = now
                    log.warning(
                        "book plan write failed, retrying",
                        extra={
                            "fields": {
                                "code": "book_plan_write_failed",
                                "rebalanced_ns": plan.row.rebalanced_ns,
                                "pending": len(pending),
                                "error": type(exc).__name__,
                            }
                        },
                    )
                break
            else:
                pending.pop(0)
                log.info(
                    "book plan recorded",
                    extra={
                        "fields": {
                            "code": "book_plan_recorded",
                            "rebalanced_ns": plan.row.rebalanced_ns,
                            "coins": len(plan.coins),
                        }
                    },
                )
        await asyncio.sleep(interval_s)
