"""add decisions and decision_outcomes (decision log, #119)

Revision ID: f2b3c4d5e6a7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-14 00:00:00.000000

Both tables are insert-only. A BEFORE UPDATE trigger enforces that in the
database so no code path can revise a call after its outcome is known.

The one UPDATE the trigger lets through on ``decisions`` is a foreign-key
action nulling ``scan_pick_id`` or ``supersedes_id`` (ON DELETE SET NULL
when the referenced scan pick or decision is deleted). Postgres runs those
actions as ordinary UPDATEs, so without the exception deleting a decision
that another one supersedes would fail. Every other column must be
unchanged, and those two may only go to NULL.

DELETE is not blocked here: the API allows it within 15 minutes of
created_at and answers 409 after that.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b3c4d5e6a7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION reject_decision_log_update() RETURNS trigger AS $$
BEGIN
    -- Nested so the decisions-only column references are never evaluated
    -- for a decision_outcomes row.
    IF TG_TABLE_NAME = 'decisions' THEN
        IF (to_jsonb(NEW) - 'scan_pick_id' - 'supersedes_id')
               = (to_jsonb(OLD) - 'scan_pick_id' - 'supersedes_id')
           AND (NEW.scan_pick_id IS NOT DISTINCT FROM OLD.scan_pick_id
                OR NEW.scan_pick_id IS NULL)
           AND (NEW.supersedes_id IS NOT DISTINCT FROM OLD.supersedes_id
                OR NEW.supersedes_id IS NULL)
        THEN
            -- ON DELETE SET NULL from scan_picks / decisions.
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION '% is insert-only; record a superseding decision instead', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("decision", sa.String(length=4), nullable=False),
        sa.Column("conviction", sa.SmallInteger(), nullable=False),
        sa.Column("thesis", sa.String(length=280), nullable=False),
        sa.Column("invalidation", sa.String(length=280), nullable=False),
        sa.Column("price_at_decision", sa.Numeric(12, 4), nullable=False),
        sa.Column("spy_at_decision", sa.Numeric(12, 4), nullable=False),
        sa.Column("source", sa.String(length=10), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("scan_pick_id", sa.UUID(), nullable=True),
        sa.Column("supersedes_id", sa.UUID(), nullable=True),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("decision IN ('buy', 'pass')", name="valid_decision_call"),
        sa.CheckConstraint("conviction BETWEEN 1 AND 5", name="valid_decision_conviction"),
        sa.CheckConstraint("source IN ('scanner', 'ticker', 'manual')", name="valid_decision_source"),
        sa.ForeignKeyConstraint(["scan_pick_id"], ["scan_picks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_decisions_ticker_created_at", "decisions", ["ticker", sa.text("created_at DESC")]
    )
    op.create_index("idx_decisions_created_at", "decisions", [sa.text("created_at DESC")])

    op.create_table(
        "decision_outcomes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("horizon_sessions", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("spy_exit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("ticker_return", sa.Numeric(10, 6), nullable=True),
        sa.Column("spy_return", sa.Numeric(10, 6), nullable=True),
        sa.Column("excess_return", sa.Numeric(10, 6), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("horizon_sessions IN (21, 63, 126)", name="valid_decision_outcome_horizon"),
        sa.CheckConstraint(
            "status IN ('scored', 'truncated', 'no_data')", name="valid_decision_outcome_status"
        ),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_id", "horizon_sessions", name="uq_decision_outcomes_decision_horizon"
        ),
    )

    op.execute(_TRIGGER_FUNCTION)
    for table in ("decisions", "decision_outcomes"):
        op.execute(
            f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_decision_log_update()"
        )


def downgrade() -> None:
    for table in ("decision_outcomes", "decisions"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_insert_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_decision_log_update()")
    op.drop_table("decision_outcomes")
    op.drop_index("idx_decisions_created_at", table_name="decisions")
    op.drop_index("idx_decisions_ticker_created_at", table_name="decisions")
    op.drop_table("decisions")
