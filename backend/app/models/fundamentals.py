from datetime import date, datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FundamentalsSnapshot(Base):
    """Cached quarterly statements for one ticker (funnel quality gate).

    Stores the raw FMP statement lists rather than derived ratios, so the
    gate is always recomputed by ``funnel.compute_quality_metrics`` — one
    code path for live and backtest, and any as-of date. ``quality_pass`` /
    ``quality_fails`` are the verdict at ``fetched_at`` for quick queries.
    Refreshed in place (statements change quarterly; a weekly refresh is
    plenty), hence ``updated_at``.
    """

    __tablename__ = "fundamentals_snapshots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    sector: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    income: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    balance: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    cashflow: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    latest_filing_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    quality_pass: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    quality_fails: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (UniqueConstraint("ticker", name="uq_fundamentals_snapshots_ticker"),)
