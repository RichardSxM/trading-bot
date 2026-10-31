"""covers: spec 0026 AC-17 (``studies/maker_figures.py``: the entry fill share leaves refused rows out, a part filled
exit counts as a fallback, and the bundle and CSV sources give the same figures)."""

import csv
import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_core.recording.bundle_codec import bundle_json
from trading_core.recording.enums import (
    Env,
    Intent,
    Liquidity,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskVerdict,
    RunMode,
    TimeInForce,
)
from trading_core.recording.rows import DecisionRow, FillRow, OrderRow, RunBundle, RunRow

_SPEC = importlib.util.spec_from_file_location(
    "maker_figures", Path(__file__).resolve().parents[1] / "studies" / "maker_figures.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
mf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mf)

D = Decimal
RUN = uuid.uuid7()


def _decision(intent, reason, approved=True, risk_reason=None):
    return DecisionRow(
        env=Env.DEMO,
        run_id=RUN,
        instrument_id="PF_XBTUSD.KRAKEN",
        intent=intent,
        side=OrderSide.BUY,
        reason=reason,
        inputs={},
        risk_verdict=RiskVerdict.APPROVED if approved else RiskVerdict.REJECTED,
        risk_reason=risk_reason,
        decided_ns=1_800_000_000_000_000_000,
    )


def _order(cid, decision, kind, qty, filled, status, reduce_only=False):
    return OrderRow(
        env=Env.DEMO,
        run_id=RUN,
        decision_id=decision.id,
        origin=OrderOrigin.STRATEGY,
        client_order_id=cid,
        instrument_id="PF_XBTUSD.KRAKEN",
        side=OrderSide.BUY,
        order_type=kind,
        time_in_force=TimeInForce.GTC,
        quantity=D(qty),
        price=D(100) if kind == OrderType.LIMIT else None,
        reduce_only=reduce_only,
        post_only=kind == OrderType.LIMIT,
        status=status,
        filled_qty=D(filled),
        last_event_ns=1_800_000_000_000_000_000,
    )


def _fill(cid, qty, liquidity=Liquidity.MAKER, fee="0.01"):
    return FillRow(
        env=Env.DEMO,
        run_id=RUN,
        client_order_id=cid,
        instrument_id="PF_XBTUSD.KRAKEN",
        trade_id=f"T-{cid}-{qty}",
        side=OrderSide.BUY,
        quantity=D(qty),
        price=D(100),
        fee=D(fee),
        fee_currency="USD",
        liquidity=liquidity,
        filled_ns=1_800_000_000_000_000_000,
    )


def _fixture():
    full, part, none, rejected = (_decision(Intent.ENTER, "donchian_long") for _ in range(4))
    vol = _decision(Intent.ENTER, "donchian_long", approved=False, risk_reason="vol_low")
    wide = _decision(Intent.ENTER, "donchian_long", approved=False, risk_reason="spread_wide")
    maker_exit = _decision(Intent.EXIT, "midline_exit")
    part_exit = _decision(Intent.EXIT, "time_stop")
    stop_exit = _decision(Intent.EXIT, "stop_through")  # not a planned exit
    decisions = [full, part, none, rejected, vol, wide, maker_exit, part_exit, stop_exit]
    L, M, R = OrderType.LIMIT, OrderType.MARKET, True
    orders = [
        _order("E1", full, L, "1", "1", OrderStatus.FILLED),
        _order("E2", part, L, "1", "0.4", OrderStatus.CANCELED),
        _order("E3", none, L, "1", "0", OrderStatus.CANCELED),
        _order("E4", rejected, L, "1", "0", OrderStatus.REJECTED),
        _order("X1", maker_exit, L, "1", "1", OrderStatus.FILLED, R),
        _order("X2", part_exit, L, "0.4", "0.1", OrderStatus.CANCELED, R),
        _order("X3", part_exit, M, "0.3", "0.3", OrderStatus.FILLED, R),
    ]
    fills = [
        _fill("E1", "1"),
        _fill("E2", "0.4"),
        _fill("X1", "1"),
        _fill("X2", "0.1"),
        _fill("X3", "0.3", Liquidity.TAKER, fee="0.05"),
    ]
    return decisions, orders, fills


def test_the_figures_leave_refused_entries_out_and_count_a_part_filled_exit_as_a_fallback():
    decisions, orders, fills = _fixture()

    figures = mf.maker_figures(*mf.from_rows(decisions, orders, fills))

    assert (figures.approved_entries, figures.entries_filled_full, figures.entries_filled_part) == (4, 1, 1)
    assert (figures.entries_unfilled, figures.entries_rejected) == (2, 1)
    assert figures.entry_fill_share == D(2) / D(4)  # vol_low and spread_wide are not in it
    assert (figures.planned_exits, figures.maker_exits, figures.fallback_exits) == (2, 1, 1)
    assert figures.maker_exit_share == D(1) / D(2)
    assert figures.skips == {"vol_low": 1, "spread_wide": 1, "bad_signal": 0}
    assert figures.fees_total == D("0.09")
    assert figures.fees_per_round_trip == D("0.045")
    assert figures.limit_taker_fills == 0


def _bundle(decisions, orders, fills):
    run = RunRow(
        id=RUN,
        env=Env.DEMO,
        mode=RunMode.BACKTEST,
        strategy_version_id=uuid.uuid7(),
        trader_id="LAB-001",
        run_config={},
        started_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    return RunBundle(run=run, decisions=decisions, orders=orders, fills=fills)


def _write_csv(folder, decisions, orders, fills):
    # As the module docstring's COPY queries write them (Postgres booleans as t and f, NULL as empty).
    tables = {
        "decisions.csv": [
            {"id": d.id, "intent": d.intent, "reason": d.reason, "risk_verdict": d.risk_verdict,
             "risk_reason": d.risk_reason or ""}
            for d in decisions
        ],
        "orders.csv": [
            {"client_order_id": o.client_order_id, "decision_id": o.decision_id or "", "order_type": o.order_type,
             "reduce_only": "t" if o.reduce_only else "f", "status": o.status, "quantity": o.quantity,
             "filled_qty": o.filled_qty}
            for o in orders
        ],
        "fills.csv": [
            {"client_order_id": f.client_order_id, "quantity": f.quantity, "fee": f.fee, "liquidity": f.liquidity}
            for f in fills
        ],
    }  # fmt: skip
    for name, rows in tables.items():
        with (folder / name).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def test_the_bundle_and_csv_sources_give_the_same_figures(tmp_path):
    decisions, orders, fills = _fixture()
    bundle = tmp_path / "run.bundle.json"
    bundle.write_text(bundle_json(_bundle(decisions, orders, fills)))
    _write_csv(tmp_path, decisions, orders, fills)

    from_bundle = mf.maker_figures(*mf.from_bundle(bundle))
    from_csv = mf.maker_figures(*mf.from_csv(tmp_path))

    assert from_bundle == from_csv == mf.maker_figures(*mf.from_rows(decisions, orders, fills))


def test_no_entries_gives_no_share():
    figures = mf.maker_figures([], [], [])
    assert (figures.entry_fill_share, figures.maker_exit_share, figures.fees_per_round_trip) == (None, None, None)
