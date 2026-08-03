"""Builds the merged journal feed: app-authored rows + vault projections.

Shared by ``GET /api/journal`` and the dashboard's recent-activity list so
both surfaces show the same picture (the vault is where Dane actually
journals; the ``journal_entries`` table alone is nearly empty).
"""

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.journal import JournalEntry
from app.models.trade_campaigns import TradeCampaign
from app.models.watchlist import WatchlistItem
from app.schemas.journal import JournalFeedEntry
from app.services.campaign_journal import parse_campaign_entries


async def build_journal_feed(
    db: AsyncSession,
    ticker: Optional[str] = None,
    entry_type: Optional[str] = None,
    limit: int = 100,
) -> list[JournalFeedEntry]:
    ticker_col = func.coalesce(JournalEntry.ticker, WatchlistItem.ticker).label(
        "ticker"
    )
    query = (
        select(JournalEntry, ticker_col)
        .outerjoin(WatchlistItem, JournalEntry.watchlist_id == WatchlistItem.id)
        .order_by(JournalEntry.created_at.desc())
    )
    if ticker:
        query = query.where(ticker_col == ticker.upper())
    if entry_type:
        query = query.where(JournalEntry.entry_type == entry_type)
    result = await db.execute(query.limit(limit))

    feed = [
        JournalFeedEntry(
            id=str(row.JournalEntry.id),
            source="app",
            watchlist_id=row.JournalEntry.watchlist_id,
            ticker=row.ticker,
            title=row.JournalEntry.title,
            entry_type=row.JournalEntry.entry_type,
            content=row.JournalEntry.content,
            created_at=row.JournalEntry.created_at,
            updated_at=row.JournalEntry.updated_at,
        )
        for row in result.all()
    ]

    # Archived campaigns are files deleted from the vault — the mirror
    # respects the deletion, so they don't feed the journal either.
    campaign_query = select(TradeCampaign).where(TradeCampaign.status != "archived")
    if ticker:
        campaign_query = campaign_query.where(TradeCampaign.ticker == ticker.upper())
    campaigns = (await db.execute(campaign_query)).scalars().all()
    for campaign in campaigns:
        feed.extend(parse_campaign_entries(campaign))

    if entry_type:
        feed = [e for e in feed if e.entry_type == entry_type]
    feed.sort(key=lambda e: e.created_at, reverse=True)
    return feed[:limit]
