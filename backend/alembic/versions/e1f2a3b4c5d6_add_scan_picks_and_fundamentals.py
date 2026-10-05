"""add scan_picks and fundamentals_snapshots

Revision ID: e1f2a3b4c5d6
Revises: b4d5e6f7a8b9
Create Date: 2026-10-03 00:00:00.000000

scan_picks: immutable forward record of each scan's top picks (ADR-0007).
fundamentals_snapshots: cached raw quarterly statements for the funnel's
quality gate.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "b4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_picks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("scan_id", sa.UUID(), nullable=False),
        sa.Column("universe", sa.String(length=30), nullable=False),
        sa.Column("ranker", sa.String(length=20), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("score", sa.Numeric(10, 3), nullable=True),
        sa.Column("setup", sa.String(length=40), nullable=True),
        sa.Column("tier", sa.String(length=2), nullable=True),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ranker IN ('scanner', 'funnel')", name="valid_scan_pick_ranker"),
        sa.CheckConstraint("rank >= 1", name="scan_pick_rank_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scan_id", "ranker", "rank", name="uq_scan_picks_scan_ranker_rank"),
    )
    op.create_index("idx_scan_picks_scanned_at", "scan_picks", [sa.text("scanned_at DESC")])
    op.create_index(
        "idx_scan_picks_ticker_scanned_at", "scan_picks", ["ticker", sa.text("scanned_at DESC")]
    )

    op.create_table(
        "fundamentals_snapshots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("sector", sa.String(length=60), nullable=True),
        sa.Column("income", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("balance", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("cashflow", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("latest_filing_date", sa.Date(), nullable=True),
        sa.Column("quality_pass", sa.Boolean(), nullable=True),
        sa.Column(
            "quality_fails", postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", name="uq_fundamentals_snapshots_ticker"),
    )


def downgrade() -> None:
    op.drop_table("fundamentals_snapshots")
    op.drop_index("idx_scan_picks_ticker_scanned_at", table_name="scan_picks")
    op.drop_index("idx_scan_picks_scanned_at", table_name="scan_picks")
    op.drop_table("scan_picks")
