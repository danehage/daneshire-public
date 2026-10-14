"""Decision log schemas (#119).

``DecisionCreate`` forbids extra fields: the price, the SPY price and the
timestamp are set by the server, and a client that tries to send them gets
a 422 instead of having them silently ignored.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionCall = Literal["buy", "pass"]
DecisionSource = Literal["scanner", "ticker", "manual"]
OutcomeStatus = Literal["scored", "truncated", "no_data"]

HORIZONS: tuple[int, ...] = (21, 63, 126)


class DecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(..., min_length=1, max_length=10, pattern=r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
    decision: DecisionCall
    conviction: int = Field(..., ge=1, le=5)
    thesis: str = Field(..., min_length=1, max_length=280)
    invalidation: str = Field(..., min_length=1, max_length=280)
    source: DecisionSource = "manual"
    # The scan the call was made from. The server resolves it to a
    # scan_picks row when the ticker is in that scan's stored record.
    scan_id: Optional[str] = Field(default=None, max_length=64)
    supersedes_id: Optional[UUID] = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("thesis", "invalidation")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        return [t.strip()[:40] for t in v if t and t.strip()]


class DecisionOutcomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    horizon_sessions: int
    status: OutcomeStatus
    exit_date: Optional[date] = None
    exit_price: Optional[Decimal] = None
    spy_exit_price: Optional[Decimal] = None
    ticker_return: Optional[Decimal] = None
    spy_return: Optional[Decimal] = None
    excess_return: Optional[Decimal] = None
    scored_at: datetime


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticker: str
    decision: DecisionCall
    conviction: int
    thesis: str
    invalidation: str
    price_at_decision: Decimal
    spy_at_decision: Decimal
    source: DecisionSource
    scan_pick_id: Optional[UUID] = None
    supersedes_id: Optional[UUID] = None
    # Set when a later decision supersedes this one (display only; a
    # superseded call still counts in the summary).
    superseded_by_id: Optional[UUID] = None
    tags: list[str]
    created_at: datetime
    # Until this moment the decision can still be deleted (15-minute grace).
    deletable_until: datetime
    outcomes: list[DecisionOutcomeResponse] = []


class DecisionSummaryCell(BaseModel):
    """Stats for one buy/pass x conviction x horizon cell.

    Counts ``scored`` and ``truncated`` outcomes; ``no_data`` rows are
    excluded. ``hit_rate`` is the share with excess return > 0.
    """

    decision: DecisionCall
    conviction: int
    horizon_sessions: int
    n: int
    mean_excess_return: Optional[float] = None
    median_excess_return: Optional[float] = None
    hit_rate: Optional[float] = None


class DecisionSummaryResponse(BaseModel):
    horizons: list[int]
    # Cells under this n are shown greyed out: too few to read anything into.
    min_reliable_n: int
    total_decisions: int
    cells: list[DecisionSummaryCell]


class DecisionScoreSummary(BaseModel):
    """Result of one ``POST /api/internal/decisions/score`` run."""

    decisions_checked: int = 0
    scored: int = 0
    truncated: int = 0
    no_data: int = 0
    # Horizons not yet due, or waiting on data (ticker data lagging, or the
    # provider failed for that ticker on this run).
    pending: int = 0
    tickers_unavailable: list[str] = []
