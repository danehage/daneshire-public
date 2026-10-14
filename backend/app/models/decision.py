from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Decision(Base):
    """One buy/pass call, frozen at the moment it was made (#119).

    Insert-only: a ``BEFORE UPDATE`` trigger rejects every update except the
    foreign-key actions that null out ``scan_pick_id`` / ``supersedes_id``
    when the row they point at is deleted. ``price_at_decision`` and
    ``spy_at_decision`` are set by the server from one quote request, and
    ``created_at`` is the decision timestamp; the client can supply neither.
    A change of mind is a new row with ``supersedes_id`` set.
    """

    __tablename__ = "decisions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    decision: Mapped[str] = mapped_column(String(4), nullable=False)
    conviction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    thesis: Mapped[str] = mapped_column(String(280), nullable=False)
    invalidation: Mapped[str] = mapped_column(String(280), nullable=False)
    price_at_decision: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    spy_at_decision: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    source: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'manual'")
    )
    scan_pick_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("scan_picks.id", ondelete="SET NULL"),
        nullable=True,
    )
    supersedes_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    outcomes: Mapped[list["DecisionOutcome"]] = relationship(
        back_populates="decision",
        order_by="DecisionOutcome.horizon_sessions",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("decision IN ('buy', 'pass')", name="valid_decision_call"),
        CheckConstraint("conviction BETWEEN 1 AND 5", name="valid_decision_conviction"),
        CheckConstraint(
            "source IN ('scanner', 'ticker', 'manual')", name="valid_decision_source"
        ),
        Index("idx_decisions_ticker_created_at", "ticker", text("created_at DESC")),
        Index("idx_decisions_created_at", text("created_at DESC")),
    )


class DecisionOutcome(Base):
    """One horizon's score for one Decision. Insert-only, like ``decisions``.

    Returns are price returns from the decision-time quotes to the close of
    the Nth SPY session after the decision. ``excess_return`` is ticker minus
    SPY and is never sign-flipped for passes.
    """

    __tablename__ = "decision_outcomes"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    decision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    horizon_sessions: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    exit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    spy_exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    ticker_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    spy_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    excess_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    decision: Mapped[Decision] = relationship(back_populates="outcomes")

    __table_args__ = (
        UniqueConstraint(
            "decision_id", "horizon_sessions", name="uq_decision_outcomes_decision_horizon"
        ),
        CheckConstraint(
            "horizon_sessions IN (21, 63, 126)", name="valid_decision_outcome_horizon"
        ),
        CheckConstraint(
            "status IN ('scored', 'truncated', 'no_data')",
            name="valid_decision_outcome_status",
        ),
    )
