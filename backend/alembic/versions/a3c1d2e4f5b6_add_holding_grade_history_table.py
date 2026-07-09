"""add holding_grade_history table

Revision ID: a3c1d2e4f5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3c1d2e4f5b6"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "holding_grade_history",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sell_grade", sa.String(20), nullable=False),
        sa.Column("sell_pressure_score", sa.Integer(), nullable=False),
        sa.Column(
            "signals",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("stop_suggestion", sa.Text(), nullable=True),
        sa.Column(
            "notified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_hgh_ticker_checked",
        "holding_grade_history",
        ["ticker", "checked_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_hgh_ticker_checked", table_name="holding_grade_history")
    op.drop_table("holding_grade_history")
