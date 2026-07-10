"""add hv_now and atr_pct to holding_grade_history

Revision ID: b4d5e6f7a8b9
Revises: c9d1e3f5a7b2
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d5e6f7a8b9"
down_revision: Union[str, None] = "c9d1e3f5a7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "holding_grade_history",
        sa.Column("hv_now", sa.Numeric(5, 1), nullable=True),
    )
    op.add_column(
        "holding_grade_history",
        sa.Column("atr_pct", sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("holding_grade_history", "atr_pct")
    op.drop_column("holding_grade_history", "hv_now")
