"""The maker figures of a ``donchian_scalp_r2`` run (spec 0026 AC-17, AC-18), from its decisions, orders, and fills.

Two sources, one count:

  python maker_figures.py bundle <run.bundle.json>     a lab run bundle (the candle screens, AC-17)
  python maker_figures.py csv <dir>                    decisions.csv, orders.csv, fills.csv (the VPS demo, AC-18)

Run the bundle form in the lab image with ``-I``. For the demo week, export r2's paper run rows from the VPS
Postgres read only (``tailscale ssh kfs@kfs-vps``, then ``psql`` in the Postgres container as ``dashboard_app``),
one ``COPY ... TO STDOUT WITH CSV HEADER`` per file, ``:'run'`` being the paper run id:

  decisions.csv: SELECT id, intent, reason, risk_verdict, risk_reason FROM decisions WHERE run_id = :'run'
  orders.csv:    SELECT client_order_id, decision_id, order_type, reduce_only, status, quantity, filled_qty
                 FROM orders WHERE run_id = :'run'
  fills.csv:     SELECT o.client_order_id, f.quantity, f.fee, f.liquidity FROM fills f
                 JOIN orders o ON o.id = f.order_id WHERE f.run_id = :'run'

What it counts, as the spec defines it:

- **Entry fill share**: approved ``enter`` decisions whose entry limit (the decision's order that is not reduce
  only) filled any part, over all approved ``enter`` decisions. Refused rows (``vol_low``, ``spread_wide``,
  ``bad_signal``, a risk cap) are not in it; they are counted on their own.
- **Maker exit share**: planned exit decisions (``midline_exit``, ``time_stop``) whose reduce only exit limit
  filled the whole position with no market order under the same decision, over all planned exit decisions.
  A part filled exit, a market fallback, and a market only exit (a stale quote, a resumed time stop) count as
  fallbacks.
- **Fees per round trip**: every fill's fee over the entries that filled. ``limit_taker_fills`` counts limit
  fills the venue marked taker; it should be 0, since both limits are post only.

The candle lab fills a limit at the touch from synthetic quotes with no queue, so its maker exit share is an
upper bound.
"""

import csv
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_core.recording.rows import DecisionRow, FillRow, OrderRow

PLANNED_EXITS = frozenset({"midline_exit", "time_stop"})
SKIPS = ("vol_low", "spread_wide", "bad_signal")


@dataclass(frozen=True)
class Decision:
    """The decision columns the figures need."""

    id: str
    intent: str
    reason: str
    risk_verdict: str
    risk_reason: str | None


@dataclass(frozen=True)
class Order:
    """The order columns the figures need."""

    client_order_id: str
    decision_id: str | None
    order_type: str
    reduce_only: bool
    status: str
    quantity: Decimal
    filled_qty: Decimal


@dataclass(frozen=True)
class Fill:
    """The fill columns the figures need."""

    client_order_id: str
    quantity: Decimal
    fee: Decimal
    liquidity: str


@dataclass(frozen=True)
class MakerFigures:
    """The AC-17 and AC-18 counts of one run."""

    approved_entries: int
    entries_filled_full: int
    entries_filled_part: int
    entries_unfilled: int
    entries_rejected: int
    entry_fill_share: Decimal | None
    planned_exits: int
    maker_exits: int
    fallback_exits: int
    maker_exit_share: Decimal | None
    skips: dict[str, int]
    fees_total: Decimal
    fees_per_round_trip: Decimal | None
    limit_taker_fills: int


def _share(part: int, whole: int) -> Decimal | None:
    return None if whole == 0 else Decimal(part) / Decimal(whole)


def maker_figures(decisions: Iterable[Decision], orders: Iterable[Order], fills: Iterable[Fill]) -> MakerFigures:
    """Count the maker figures (module docstring) from one run's rows."""
    decisions, orders, fills = list(decisions), list(orders), list(fills)
    by_decision: dict[str, list[Order]] = defaultdict(list)
    for order in orders:
        if order.decision_id is not None:
            by_decision[order.decision_id].append(order)
    limits = {o.client_order_id for o in orders if o.order_type == "limit"}

    approved = [d for d in decisions if d.intent == "enter" and d.risk_verdict == "approved"]
    full = part = unfilled = rejected = 0
    for decision in approved:
        entries = [o for o in by_decision[decision.id] if not o.reduce_only]
        filled = sum((o.filled_qty for o in entries), Decimal(0))
        ordered = sum((o.quantity for o in entries), Decimal(0))
        if any(o.status == "rejected" for o in entries):
            rejected += 1
        if filled == 0:
            unfilled += 1  # also an entry the gate stopped before it went out
        elif filled == ordered:
            full += 1
        else:
            part += 1

    planned = [d for d in decisions if d.intent == "exit" and d.reason in PLANNED_EXITS]
    maker = 0
    for decision in planned:
        under = by_decision[decision.id]
        exit_limits = [o for o in under if o.order_type == "limit" and o.reduce_only]
        markets = [o for o in under if o.order_type == "market" and o.filled_qty > 0]
        if exit_limits and not markets and all(o.filled_qty == o.quantity for o in exit_limits):
            maker += 1

    fees = sum((f.fee for f in fills), Decimal(0))
    round_trips = full + part
    return MakerFigures(
        approved_entries=len(approved),
        entries_filled_full=full,
        entries_filled_part=part,
        entries_unfilled=unfilled,
        entries_rejected=rejected,
        entry_fill_share=_share(round_trips, len(approved)),
        planned_exits=len(planned),
        maker_exits=maker,
        fallback_exits=len(planned) - maker,
        maker_exit_share=_share(maker, len(planned)),
        skips={reason: sum(1 for d in decisions if d.risk_reason == reason) for reason in SKIPS},
        fees_total=fees,
        fees_per_round_trip=None if round_trips == 0 else fees / round_trips,
        limit_taker_fills=sum(1 for f in fills if f.client_order_id in limits and f.liquidity == "taker"),
    )


# Sources ----------------------------------------------------------------------------------------


def from_rows(
    decisions: Iterable[DecisionRow], orders: Iterable[OrderRow], fills: Iterable[FillRow]
) -> tuple[list[Decision], list[Order], list[Fill]]:
    """The figure columns of recording rows (a bundle's, or rows built in a test)."""
    return (
        [Decision(str(d.id), str(d.intent), d.reason, str(d.risk_verdict), d.risk_reason) for d in decisions],
        [
            Order(
                o.client_order_id,
                None if o.decision_id is None else str(o.decision_id),
                str(o.order_type),
                o.reduce_only,
                str(o.status),
                o.quantity,
                o.filled_qty,
            )
            for o in orders
        ],
        [Fill(f.client_order_id, f.quantity, f.fee, str(f.liquidity)) for f in fills],
    )


def from_bundle(path: Path) -> tuple[list[Decision], list[Order], list[Fill]]:
    """The rows of a lab run bundle, streamed (a screen's bundle can be large)."""
    from trading_core.recording.bundle_codec import BundleFile
    from trading_core.recording.rows import DecisionRow, FillRow, OrderRow

    bundle = BundleFile(path)
    return from_rows(
        bundle.rows("decisions", DecisionRow), bundle.rows("orders", OrderRow), bundle.rows("fills", FillRow)
    )


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def from_csv(folder: Path) -> tuple[list[Decision], list[Order], list[Fill]]:
    """The rows of the three CSV files the module docstring's queries export."""
    decisions = [
        Decision(r["id"], r["intent"], r["reason"], r["risk_verdict"], r["risk_reason"] or None)
        for r in _csv(folder / "decisions.csv")
    ]
    orders = [
        Order(
            r["client_order_id"],
            r["decision_id"] or None,
            r["order_type"],
            r["reduce_only"] in ("t", "true", "True"),
            r["status"],
            Decimal(r["quantity"]),
            Decimal(r["filled_qty"]),
        )
        for r in _csv(folder / "orders.csv")
    ]
    fills = [
        Fill(r["client_order_id"], Decimal(r["quantity"]), Decimal(r["fee"]), r["liquidity"])
        for r in _csv(folder / "fills.csv")
    ]
    return decisions, orders, fills


def main(argv: Sequence[str]) -> int:
    """Print the figures as JSON; 64 on bad arguments."""
    if len(argv) != 2 or argv[0] not in ("bundle", "csv"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 64
    rows = from_bundle(Path(argv[1])) if argv[0] == "bundle" else from_csv(Path(argv[1]))
    print(json.dumps(asdict(maker_figures(*rows)), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
