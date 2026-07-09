"""add trade_campaigns table

Revision ID: b7e2f4a9c1d3
Revises: a3c1d2e4f5b6
Create Date: 2026-07-09 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e2f4a9c1d3"
down_revision: Union[str, None] = "a3c1d2e4f5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_campaigns",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("vault_path", sa.Text(), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column(
            "frontmatter",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("opened_on", sa.Date(), nullable=True),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("cost_basis", sa.Numeric(12, 4), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("account", sa.String(30), nullable=True),
        sa.Column("strategy", sa.String(30), nullable=True),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title", name="uq_trade_campaigns_title"),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'archived')",
            name="valid_campaign_status",
        ),
    )
    op.create_index("idx_trade_campaigns_ticker", "trade_campaigns", ["ticker"])
    op.create_index("idx_trade_campaigns_status", "trade_campaigns", ["status"])


def downgrade() -> None:
    op.drop_index("idx_trade_campaigns_status", table_name="trade_campaigns")
    op.drop_index("idx_trade_campaigns_ticker", table_name="trade_campaigns")
    op.drop_table("trade_campaigns")
