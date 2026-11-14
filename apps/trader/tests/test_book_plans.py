"""covers: spec 0033 AC-6 (the ``write_book_plans`` task: rows built once from the first snapshot of a rebalance,
the same frozen rows retried after a failure even when the snapshot moved on, a write failure logged at most once
a minute, a conflict reported and dropped, nothing written without a plan, rebalance time, or equity, coin rows
only for plan items, and null vol fields for a book that is not vol sized)."""

import asyncio
import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from trader.book_plans import book_plan_rows, write_book_plans
from trading_core.recording.book_plan_rows import BookPlanReason
from trading_core.recording.enums import Env
from trading_core.recording.errors import RecordConflictError

D = Decimal
RUN = uuid4()
BTC, ETH, SOL = "PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN"


def _coin(instrument, weight="1.2", **overrides):
    values = {
        "instrument_id": instrument,
        "eligible": True,
        "signal": 1,
        "vol": "0.03",
        "weight": weight,
        "target_notional": "12000",
        "target_qty": "0.2",
        "close_date": "2026-11-13",
        "failed": False,
    }
    return values | overrides


def _snapshot(rebalanced_ns=1_800_000_000_000_000_000, equity="10000", **overrides):
    values = {
        "coins": [
            _coin(BTC),
            _coin(ETH, weight="-0.4", signal=-1),
            _coin(SOL, eligible=None, signal=None, vol=None, weight=None, target_notional=None, close_date=None),
        ],
        "n": 2,
        "gross": "0.8",
        "gross_scale": "1",
        "equity": equity,
        "plan_day": "2026-11-13",
        "last_rebalance_at": rebalanced_ns,
        "last_reason": "daily",
        "failed": [],
        "run_at_utc": "00:05",
        "schedule": "daily",
        "book_vol_target": "0.5",
        "rebalance_band": "0.3",
        "unit_vol": "0.021",
        "book_gross": "1.6",
    }
    return values | overrides


def test_the_rows_follow_the_snapshot_and_skip_coins_without_a_plan_item():
    frozen = book_plan_rows(_snapshot(), Env.DEMO, RUN, D(5))

    assert frozen is not None
    row = frozen.row
    assert (row.run_id, row.plan_day, row.reason, row.n) == (RUN, date(2026, 11, 13), BookPlanReason.DAILY, 2)
    assert (row.equity, row.gross, row.gross_cap) == (D(10000), D("0.8"), D(5))
    assert (row.unit_vol, row.book_gross, row.book_vol_target, row.rebalance_band) == (
        D("0.021"),
        D("1.6"),
        D("0.5"),
        D("0.3"),
    )
    assert [c.instrument_id for c in frozen.coins] == [BTC, ETH]
    assert (frozen.coins[1].signal, frozen.coins[1].weight) == (-1, D("-0.4"))


def test_a_book_that_is_not_vol_sized_gets_null_vol_fields():
    snapshot = {k: v for k, v in _snapshot().items() if k not in {"unit_vol", "book_gross", "book_vol_target"}}
    snapshot.pop("rebalance_band")

    frozen = book_plan_rows(snapshot, Env.DEMO, RUN, D(2))

    assert frozen is not None
    assert (frozen.row.unit_vol, frozen.row.book_gross, frozen.row.book_vol_target, frozen.row.rebalance_band) == (
        None,
    ) * 4


@pytest.mark.parametrize("missing", ["last_rebalance_at", "equity", "plan_day"])
def test_no_plan_no_rebalance_or_no_equity_writes_nothing(missing):
    assert book_plan_rows(_snapshot(**{missing: None}), Env.DEMO, RUN, D(5)) is None


class _Feed:
    """A snapshot per tick; the task is cancelled once they run out."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def __call__(self):
        if not self.snapshots:
            raise asyncio.CancelledError
        return self.snapshots.pop(0)


def _no_sessions():
    # The fake ``write`` never opens a session.
    raise AssertionError


def _run(snapshots, write, monotonic=lambda: 0.0):
    async def go():
        await write_book_plans(
            _Feed(snapshots), Env.DEMO, RUN, D(5), _no_sessions, interval_s=0, monotonic=monotonic, write=write
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())


def test_each_rebalance_is_written_once():
    written = []

    _run([_snapshot(), _snapshot(), None, _snapshot(rebalanced_ns=2)], lambda s, plan: written.append(plan))

    assert [p.row.rebalanced_ns for p in written] == [1_800_000_000_000_000_000, 2]


def test_a_failed_write_retries_the_frozen_rows_not_a_fresh_read(caplog):
    attempts = []

    def write(sessions, plan):
        attempts.append(plan)
        if len(attempts) < 3:
            raise OSError("postgres down")

    # The plan is replaced (a new equity) at the same rebalance time while the write keeps failing.
    snapshots = [_snapshot(equity="10000"), _snapshot(equity="9000"), _snapshot(equity="8000")]
    with caplog.at_level(logging.WARNING):
        _run(snapshots, write)

    assert len(attempts) == 3
    assert {p.row.equity for p in attempts} == {D(10000)}
    assert len({id(p) for p in attempts}) == 1
    failures = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "book_plan_write_failed"]
    assert len(failures) == 1  # at most one warning a minute


def test_the_warning_repeats_after_a_minute(caplog):
    clock = iter([0.0, 30.0, 61.0])

    def write(sessions, plan):
        raise OSError("postgres down")

    with caplog.at_level(logging.WARNING):
        _run([_snapshot()] * 3, write, monotonic=lambda: next(clock))

    failures = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "book_plan_write_failed"]
    assert len(failures) == 2


def test_a_newer_rebalance_waits_behind_a_failing_one():
    attempts = []

    def write(sessions, plan):
        attempts.append(plan.row.rebalanced_ns)
        if len(attempts) == 1:
            raise OSError("postgres down")

    _run([_snapshot(rebalanced_ns=1), _snapshot(rebalanced_ns=2)], write)

    assert attempts == [1, 1, 2]


def test_a_conflict_is_dropped_and_the_next_plan_goes_on():
    attempts = []

    def write(sessions, plan):
        attempts.append(plan.row.rebalanced_ns)
        if plan.row.rebalanced_ns == 1:
            raise RecordConflictError("book_plans", {"rebalanced_ns": 1}, {"equity": 1}, {"equity": 2})

    _run([_snapshot(rebalanced_ns=1), _snapshot(rebalanced_ns=2), _snapshot(rebalanced_ns=2)], write)

    assert attempts == [1, 2]


def test_an_unknown_reason_is_logged_once_and_skipped(caplog):
    written = []

    with caplog.at_level(logging.ERROR):
        _run([_snapshot(last_reason="reenter")] * 2, lambda s, plan: written.append(plan))

    assert written == []
    invalid = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "book_plan_invalid"]
    assert len(invalid) == 1
