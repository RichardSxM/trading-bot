"""Alerter fx_rates grant (spec 0024 AC-16): let ``alerter_app`` read and insert ``fx_rates``.

The alerter fetches the daily ECB USD per EUR rate into ``fx_rates`` and checks how fresh it is.
Rows never change once written, so no UPDATE or DELETE. Expand only.

Revision ID: 0013_alerter_fx_rates
Revises: 0012_reconciliations
Create Date: 2026-10-30 12:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_alerter_fx_rates"
down_revision: str | None = "0012_reconciliations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT ON fx_rates TO alerter_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT ON fx_rates FROM alerter_app")
