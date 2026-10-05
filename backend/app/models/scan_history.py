from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScanPick(Base):
    """One ranked pick from one scan — the scanner's forward record.

    Immutable: written once when a scan completes, never updated, so the
    record of what was picked (and at what price) can't be revised after
    the outcome is known. ``ranker`` distinguishes the live scanner score
    from the conviction funnel once it runs alongside (ADR-0007).
    """

    __tablename__ = "scan_picks"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    scan_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    universe: Mapped[str] = mapped_column(String(30), nullable=False)
    ranker: Mapped[str] = mapped_column(String(20), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 3), nullable=True)
    setup: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    tier: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("scan_id", "ranker", "rank", name="uq_scan_picks_scan_ranker_rank"),
        CheckConstraint("ranker IN ('scanner', 'funnel')", name="valid_scan_pick_ranker"),
        CheckConstraint("rank >= 1", name="scan_pick_rank_positive"),
        Index("idx_scan_picks_scanned_at", text("scanned_at DESC")),
        Index("idx_scan_picks_ticker_scanned_at", "ticker", text("scanned_at DESC")),
    )
