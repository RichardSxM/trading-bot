"""Trend kind (spec 0027 AC-2): ``strategies.kind`` accepts ``trend``.

Expand only: the check created in 0002 is replaced by one that also admits ``trend``, so the
previous release still works on this schema. The model's check follows ``StrategyKind``, so the
model is unchanged. The downgrade restores the two value check and refuses while a ``trend`` row
exists, so no strategy is left outside its kind check.

Revision ID: 0014_trend_kind
Revises: 0013_alerter_fx_rates
Create Date: 2026-11-01 12:00:00.000000+00:00
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0014_trend_kind"
down_revision: str | None = "0013_alerter_fx_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRATEGIES: Final = "strategies"
KIND_CHECK: Final = "ck_strategies_kind_valid"
OLD_KINDS: Final = "'scalping', 'hour_level'"
NEW_KINDS: Final = f"{OLD_KINDS}, 'trend'"


def upgrade() -> None:
    op.drop_constraint(op.f(KIND_CHECK), STRATEGIES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), STRATEGIES, f"kind IN ({NEW_KINDS})")


def downgrade() -> None:
    trend_rows: int = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM strategies WHERE kind = 'trend'")).scalar_one()
    )
    if trend_rows:
        raise RuntimeError(
            f"refusing to downgrade 0014_trend_kind: {trend_rows} strategies row(s) of kind 'trend' exist; "
            "the two value kind check would reject them"
        )
    op.drop_constraint(op.f(KIND_CHECK), STRATEGIES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), STRATEGIES, f"kind IN ({OLD_KINDS})")
