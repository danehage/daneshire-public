from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TradeCampaignResponse(BaseModel):
    """List-view shape: everything except the (large) markdown body."""

    id: UUID
    title: str
    vault_path: str
    ticker: str
    status: str
    opened_on: Optional[date] = None
    closed_on: Optional[date] = None
    cost_basis: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    account: Optional[str] = None
    strategy: Optional[str] = None
    frontmatter: dict = {}
    parse_error: Optional[str] = None
    synced_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TradeCampaignDetailResponse(TradeCampaignResponse):
    body: str


class VaultSyncFile(BaseModel):
    path: str = Field(
        description="Vault-relative path, e.g. 'Open/UPS-2026-04.md'",
        max_length=500,
    )
    content: str


class VaultSyncRequest(BaseModel):
    """One full listing of the vault's campaign files.

    The sync is one-way and whole-vault: campaigns in the DB but absent from
    ``files`` are soft-archived.
    """

    files: list[VaultSyncFile]


class VaultSyncResponse(BaseModel):
    upserted: list[str] = []
    unchanged: int = 0
    archived: list[str] = []
    errors: dict[str, str] = {}
    watchlist_updated: list[str] = []
