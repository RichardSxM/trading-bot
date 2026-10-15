"""covers: spec 0008 AC-16 (units start in plan order, at most ``workers`` at a time, each in its own process)
and AC-13 (failing a variant drops its pending units and stops its running ones)."""

import os
import time
from decimal import Decimal
from pathlib import Path

from lab.pool import UnitDone, UnitFailed, UnitPool
from lab.units import UnitPlan, UnitResult
from trading_core.lab.errors import UnitFailedError
from trading_core.lab.segments import Segment
from trading_core.risk.check import RiskLimits

from .conftest import H17, INSTRUMENT


def _plan(root: Path, variant: str, index: int) -> UnitPlan:
    return UnitPlan(
        variant=variant,
        segment=Segment(index=index, hours=(H17,)),
        source_root=root,
        instrument_id=INSTRUMENT,
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        latency_ms=25,
        queue_position=True,
        starting_balance_usd=Decimal(10_000),
        leverage=Decimal(2),
        risk=RiskLimits(
            max_order_notional=Decimal(2000),
            max_leverage=Decimal(2),
            max_total_exposure=Decimal(2000),
            risk_per_trade=Decimal("0.005"),
            maintenance_margin_rate=Decimal("0.01"),
            min_liq_distance_pct=Decimal(10),
            liq_stop_multiple=Decimal(3),
            stop_slippage_bps=Decimal(5),
        ),
        seed=7,
        strategy_name="random_taker",
        strategy_config={},
    )


def _result(plan: UnitPlan) -> UnitResult:
    return UnitResult(
        variant=plan.variant,
        segment=plan.segment.index,
        decisions=(),
        risk_events=(),
        orders=(),
        order_events=(),
        fills=(),
        positions=(),
        spreads={},
        funding_samples=(),
        segment_end_partial=False,
        markouts=(),
        events=0,
        last_ts_init=None,
        wall_seconds=0.0,
        max_rss_bytes=0,
    )


def mark(plan: UnitPlan) -> UnitResult:
    """Write when this unit ran, and in which process, into the plan's ``source_root``."""
    begin = time.monotonic_ns()
    time.sleep(1.5)  # longer than the spread of spawn import times
    (plan.source_root / f"{plan.variant}-{plan.segment.index}").write_text(
        f"{begin} {time.monotonic_ns()} {os.getpid()}", encoding="utf-8"
    )
    return _result(plan)


def fail_or_hang(plan: UnitPlan) -> UnitResult:
    """Variant ``a``: segment 1 raises at once, segment 2 hangs. Variant ``b`` finishes quickly."""
    if plan.variant == "a" and plan.segment.index == 1:
        raise ValueError("boom")
    if plan.variant == "a":
        time.sleep(120)
    time.sleep(0.5)
    return _result(plan)


def test_units_start_in_plan_order_at_most_workers_at_a_time_each_in_its_own_process(tmp_path):
    plans = [_plan(tmp_path, v, i) for v in ("base", "queue=optimistic") for i in (1, 2, 3)]

    outcomes = list(UnitPool(plans, 2, target=mark, env="demo").outcomes())

    assert all(isinstance(o, UnitDone) for o in outcomes)
    runs = []
    for plan in plans:
        begin, end, pid = (tmp_path / f"{plan.variant}-{plan.segment.index}").read_text().split()
        runs.append((int(begin), int(end), int(pid), plan))
    # A child's own start time includes its spawn import, so units of one wave may swap; the waves may not.
    by_start = [r[3] for r in sorted(runs, key=lambda r: r[0])]
    waves = [{(p.variant, p.segment.index) for p in by_start[i : i + 2]} for i in range(0, len(plans), 2)]
    assert waves == [{(p.variant, p.segment.index) for p in plans[i : i + 2]} for i in range(0, len(plans), 2)]
    pids = {r[2] for r in runs}
    assert len(pids) == len(plans)
    assert os.getpid() not in pids
    busiest = max(sum(1 for b, e, _, _ in runs if b <= start < e) for start, _, _, _ in runs)
    assert busiest == 2


def test_failing_a_variant_drops_and_stops_only_its_units(tmp_path):
    plans = [_plan(tmp_path, "a", 1), _plan(tmp_path, "a", 2), _plan(tmp_path, "b", 1), _plan(tmp_path, "a", 3)]
    pool = UnitPool(plans, 3, target=fail_or_hang, env="demo")
    started = time.monotonic()

    seen = []
    for outcome in pool.outcomes():
        seen.append(outcome)
        if isinstance(outcome, UnitFailed):
            pool.fail_variant(outcome.plan.variant)

    assert time.monotonic() - started < 30
    failed = [o for o in seen if isinstance(o, UnitFailed)]
    assert [(o.plan.variant, o.plan.segment.index) for o in failed] == [("a", 1)]
    assert isinstance(failed[0].error, UnitFailedError)
    assert failed[0].error.error_type == "ValueError"
    assert str(failed[0].error) == "boom"
    assert [(o.plan.variant, o.plan.segment.index) for o in seen if isinstance(o, UnitDone)] == [("b", 1)]
