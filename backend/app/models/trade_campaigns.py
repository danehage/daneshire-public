from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TradeCampaign(Base):
    """A trade journal synced one-way from the DaneBrain Obsidian vault (#61).

    The vault is authoritative; the app never writes back. Keyed by ``title``
    (the filename stem, e.g. ``UPS-2026-04``) because campaign files MOVE from
    ``Open/`` to ``Closed/`` when a trade ends — the path changes, the stem
    doesn't. ``status`` is derived from the folder; ``archived`` means the file
    disappeared from the vault entirely (soft archive, row kept).
    """

    __tablename__ = "trade_campaigns"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    vault_path: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    frontmatter: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Parsed frontmatter conveniences for list views / sorting.
    opened_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    closed_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cost_basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    account: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    strategy: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'closed', 'archived')", name="valid_campaign_status"
        ),
        Index("idx_trade_campaigns_ticker", "ticker"),
        Index("idx_trade_campaigns_status", "status"),
    )
