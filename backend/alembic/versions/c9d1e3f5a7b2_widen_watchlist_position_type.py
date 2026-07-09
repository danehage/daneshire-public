"""widen watchlist position_type to fit its own allowed values

The API schema has always allowed position_type values up to
'cash_secured_put' (16 chars), but the column was VARCHAR(10) — setting
either option-strategy value 500'd. Surfaced by the #61 vault sync
(AXON campaign strategy → 'covered_call').

Revision ID: c9d1e3f5a7b2
Revises: b7e2f4a9c1d3
Create Date: 2026-07-09 12:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1e3f5a7b2"
down_revision: Union[str, None] = "b7e2f4a9c1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "watchlist_items",
        "position_type",
        existing_type=sa.String(10),
        type_=sa.String(20),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "watchlist_items",
        "position_type",
        existing_type=sa.String(20),
        type_=sa.String(10),
        existing_nullable=True,
    )
