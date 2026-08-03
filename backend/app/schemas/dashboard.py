from pydantic import BaseModel

from app.schemas.journal import JournalFeedEntry


class WatchlistCounts(BaseModel):
    watching: int = 0
    open: int = 0
    closed: int = 0
    total: int = 0


class AlertCounts(BaseModel):
    active: int = 0
    triggered_today: int = 0
    triggered_total: int = 0


class DashboardSummary(BaseModel):
    watchlist: WatchlistCounts
    alerts: AlertCounts
    recent_journal: list[JournalFeedEntry]
