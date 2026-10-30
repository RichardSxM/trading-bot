"""The Reconciliation page (spec 0023 AC-19): its column heads, formatted rows, and pager."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from markupsafe import Markup, escape

from dashboard.ui.format import num, ts
from dashboard.web.trades import Cell, PagerView
from trading_core.recording.queries import PAGE_SIZE, MismatchLine, ReconciliationLine

DASH: Final = "—"
# (heading, is_number) for the history table.
RECONCILIATION_COLUMNS: Final = [
    ("Started", False),
    ("Trigger", False),
    ("Outcome", False),
    ("Kraken orders", True),
    ("Kraken positions", True),
    ("Findings and actions", False),
]
# An outcome's badge kind: clean is calm, a mismatch or a failed check is the alert colour.
_OUTCOME_KIND: Final = {"clean": "ok", "mismatch": "bad", "failed": "bad"}


def _words(code: str) -> str:
    return code.replace("_", " ")


def _view(side: str | None, qty: Decimal | None) -> str:
    if side is None and qty is None:
        return "none"
    return " ".join(part for part in (side, None if qty is None else num(qty)) if part)


def mismatch_text(line: MismatchLine) -> str:
    """One finding in plain words, such as
    ``unknown position PF_XBTUSD · Kraken long 0.0001 · trader none · flattened, pending``."""
    market = line.instrument_id.partition(".")[0]
    parts = [f"{_words(line.kind)} {market}"]
    if line.client_order_id is not None:
        parts.append(f"order {line.client_order_id}")
    if line.exchange_side is not None or line.exchange_qty is not None or line.local_side is not None:
        parts.append(f"Kraken {_view(line.exchange_side, line.exchange_qty)}")
        parts.append(f"trader {_view(line.local_side, line.local_qty)}")
    parts.append(f"{_words(line.action)}, {_words(line.action_result)}")
    return " · ".join(parts)


def _outcome(line: ReconciliationLine) -> Markup:
    if line.outcome is None:
        return Markup('<span class="badge badge--warn">running</span>')
    kind = _OUTCOME_KIND.get(line.outcome, "neutral")
    return Markup('<span class="badge badge--{}">{}</span>').format(kind, line.outcome)


def _findings(line: ReconciliationLine) -> Cell:
    items = [mismatch_text(m) for m in line.mismatches]
    if line.error is not None:
        items.insert(0, f"failed: {_words(line.error)}")
    if not items:
        return DASH
    return Markup('<ul class="findings">{}</ul>').format(
        Markup("").join(Markup("<li>{}</li>").format(escape(item)) for item in items)
    )


def _count(value: int | None) -> Cell:
    return DASH if value is None else num(Decimal(value))


def reconciliation_cells(line: ReconciliationLine) -> list[Cell]:
    """One table row under ``RECONCILIATION_COLUMNS``; Kraken's counts are blank for a failed check."""
    return [
        ts(line.started_at),
        line.trigger,
        _outcome(line),
        _count(line.exchange_orders),
        _count(line.exchange_positions),
        _findings(line),
    ]


def reconciliations_context(lines: Sequence[ReconciliationLine], page: int) -> dict[str, object]:
    """Everything ``pages/reconciliations.html`` renders: a full page offers ``older`` (it may be empty)."""
    return {
        "reconciliation_columns": RECONCILIATION_COLUMNS,
        "reconciliations": [reconciliation_cells(line) for line in lines],
        "reconciliation_pager": PagerView(
            page,
            newer=f"?page={page - 1}" if page > 1 else None,
            older=f"?page={page + 1}" if len(lines) >= PAGE_SIZE else None,
        ),
    }
