from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.trade_campaigns import TradeCampaign
from app.schemas.trade_campaigns import (
    TradeCampaignDetailResponse,
    TradeCampaignResponse,
    VaultSyncRequest,
    VaultSyncResponse,
)
from app.services.vault_sync import VaultSyncService

router = APIRouter(tags=["trade-campaigns"])


@router.get("/api/trade-campaigns", response_model=list[TradeCampaignResponse])
async def list_trade_campaigns(
    ticker: Optional[str] = Query(default=None, max_length=10),
    include_archived: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """List synced vault campaigns, open first, newest within group."""
    query = select(TradeCampaign)
    if ticker:
        query = query.where(TradeCampaign.ticker == ticker.upper())
    if not include_archived:
        query = query.where(TradeCampaign.status != "archived")
    result = await db.execute(
        query.order_by(TradeCampaign.opened_on.desc().nulls_last())
    )
    rows = list(result.scalars().all())
    # Open campaigns first, then closed, then archived; newest first within
    # each group (the query already ordered by opened_on and Python's sort
    # is stable).
    rank = {"open": 0, "closed": 1, "archived": 2}
    rows.sort(key=lambda r: rank.get(r.status, 3))
    return rows


@router.get(
    "/api/trade-campaigns/{campaign_id}",
    response_model=TradeCampaignDetailResponse,
)
async def get_trade_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(TradeCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trade campaign not found")
    return row


@router.post("/api/trade-campaigns/sync", response_model=VaultSyncResponse)
async def sync_vault(
    payload: VaultSyncRequest,
    db: AsyncSession = Depends(get_db),
):
    """Whole-vault one-way sync from the DaneBrain Obsidian vault.

    Called by scripts/sync_vault.py (launchd/manual) with owner Basic auth —
    the auth middleware already denies guests everything outside its
    allowlist, so no extra guard is needed here.
    """
    if len(payload.files) == 0:
        raise HTTPException(
            status_code=422,
            detail="Empty file list — refusing to archive the entire vault. "
            "If the vault really is empty, archive campaigns manually.",
        )
    service = VaultSyncService(db)
    return await service.sync(payload.files)
