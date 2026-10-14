from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


VALID_ENTRY_TYPES = ["thesis", "note", "entry", "exit", "adjustment", "review"]


class JournalEntryCreate(BaseModel):
    entry_type: str = Field(default="note", pattern="^(thesis|note|entry|exit|adjustment|review)$")
    title: Optional[str] = Field(default=None, max_length=200)
    content: str = Field(..., min_length=1)


class StandaloneJournalEntryCreate(BaseModel):
    """For creating journal entries not tied to a watchlist item."""
    ticker: str = Field(..., min_length=1, max_length=10)
    entry_type: str = Field(default="note", pattern="^(thesis|note|entry|exit|adjustment|review)$")
    title: Optional[str] = Field(default=None, max_length=200)
    content: str = Field(..., min_length=1)


class JournalEntryUpdate(BaseModel):
    entry_type: Optional[str] = Field(default=None, pattern="^(thesis|note|entry|exit|adjustment|review)$")
    title: Optional[str] = Field(default=None, max_length=200)
    content: Optional[str] = Field(default=None, min_length=1)


class JournalEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    watchlist_id: Optional[UUID] = None
    ticker: Optional[str] = None
    title: Optional[str] = None
    entry_type: str
    content: str
    created_at: datetime
    updated_at: datetime


class JournalFeedEntry(BaseModel):
    """One item in the merged journal feed (``GET /api/journal``).

    ``source="app"`` rows are real ``journal_entries`` rows (``id`` is their
    UUID, editable/deletable). ``source="vault"`` entries are parsed at read
    time from synced campaign bodies — read-only, with a synthetic
    ``vault:{campaign_id}:{section}`` id and ``campaign_id`` set for linking.
    ``source="decision"`` entries are decision-log rows (#119), read-only
    (``decision:{decision_id}``), ``entry_type="decision"``.
    """

    id: str
    source: Literal["app", "vault", "decision"] = "app"
    watchlist_id: Optional[UUID] = None
    campaign_id: Optional[UUID] = None
    decision_id: Optional[UUID] = None
    ticker: Optional[str] = None
    title: Optional[str] = None
    entry_type: str
    content: str
    created_at: datetime
    updated_at: datetime


class JournalSearchResult(BaseModel):
    """Journal entry with ticker context for search results."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    watchlist_id: Optional[UUID] = None
    ticker: Optional[str] = None
    title: Optional[str] = None
    entry_type: str
    content: str
    created_at: datetime
    updated_at: datetime
