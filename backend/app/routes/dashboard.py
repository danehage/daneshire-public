"""
Dashboard API routes for summary statistics and recent activity.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.watchlist import WatchlistItem
from app.models.alert import Alert
from app.schemas.dashboard import (
    DashboardSummary,
    WatchlistCounts,
    AlertCounts,
)
from app.services.journal_feed import build_journal_feed

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
):
    """Get dashboard summary with watchlist counts, alert counts, and recent journal entries."""

    # Get watchlist counts by status
    watchlist_result = await db.execute(
        select(WatchlistItem.status, func.count(WatchlistItem.id))
        .group_by(WatchlistItem.status)
    )
    watchlist_counts = dict(watchlist_result.all())

    watchlist = WatchlistCounts(
        watching=watchlist_counts.get("watching", 0),
        # "owned" superseded "position_open" when the vault sync landed (#61);
        # count both so open positions reflect the statuses actually in use.
        open=watchlist_counts.get("owned", 0)
        + watchlist_counts.get("position_open", 0),
        closed=watchlist_counts.get("closed", 0),
        total=sum(watchlist_counts.values()),
    )

    # Get alert counts
    active_result = await db.execute(
        select(func.count(Alert.id)).where(Alert.status == "active")
    )
    active_count = active_result.scalar() or 0

    # Triggered today (UTC)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    triggered_today_result = await db.execute(
        select(func.count(Alert.id))
        .where(Alert.status == "triggered")
        .where(Alert.triggered_at >= today_start)
    )
    triggered_today_count = triggered_today_result.scalar() or 0

    # Total triggered
    triggered_total_result = await db.execute(
        select(func.count(Alert.id)).where(Alert.status == "triggered")
    )
    triggered_total_count = triggered_total_result.scalar() or 0

    alerts = AlertCounts(
        active=active_count,
        triggered_today=triggered_today_count,
        triggered_total=triggered_total_count,
    )

    # Recent activity = the merged journal feed (app entries + vault
    # campaign projections), same source as the Journal page.
    recent_journal = await build_journal_feed(db, limit=10)

    return DashboardSummary(
        watchlist=watchlist,
        alerts=alerts,
        recent_journal=recent_journal,
    )
