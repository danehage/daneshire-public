"""add owned to watchlist status

Revision ID: b1c2d3e4f5a6
Revises: a2b3c4d5e6f7
Create Date: 2026-06-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('valid_status', 'watchlist_items', type_='check')
    op.create_check_constraint(
        'valid_status',
        'watchlist_items',
        "status IN ('watching', 'position_open', 'closed', 'owned')",
    )


def downgrade() -> None:
    op.drop_constraint('valid_status', 'watchlist_items', type_='check')
    op.create_check_constraint(
        'valid_status',
        'watchlist_items',
        "status IN ('watching', 'position_open', 'closed')",
    )
