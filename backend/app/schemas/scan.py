"""
Scanner Pydantic schemas for request/response models.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UniverseInfo(BaseModel):
    """Info about a stock universe."""

    name: str
    # None for the screener-built ``combined`` before this instance has
    # built it (the first scan does; listing never calls FMP).
    size: Optional[int] = None
    description: str


class ScanRequest(BaseModel):
    """Request to start a new scan."""

    universe: str = "combined"  # combined, sp500, sp400, sp500_sample
    use_cache: bool = True


class ScanProgressEvent(BaseModel):
    """SSE progress event during scan."""

    type: str  # "progress" or "complete"
    scan_id: str
    current: Optional[int] = None
    total: Optional[int] = None
    found: Optional[int] = None
    errors: Optional[int] = None  # Track failed analyses (rate limits, etc.)
    total_analyzed: Optional[int] = None


class ScanResultItem(BaseModel):
    """Single stock result from a scan."""

    ticker: str
    price: float
    market_cap: int
    volume: int
    avg_volume: int
    volume_pace: float
    volume_pace_reliable: bool
    hv_rank: Optional[float] = None
    current_hv: Optional[float] = None
    trend: str
    dist_50: Optional[float] = None
    dist_200: Optional[float] = None
    ma_slope: Optional[float] = None
    range_position: float
    high_52w: float
    low_52w: float
    support: float
    resistance: float
    support_type: str
    resistance_type: str
    volume_ratio: float
    rsi: float
    momentum_10d: float
    # Recovery-track metrics (issue #49) — None when the stock has <60 bars
    # of history or the metric is undefined (e.g. <2 swing lows).
    setup: Optional[str] = None
    days_since_low: Optional[int] = None
    pct_above_low: Optional[float] = None
    rsi_floor_delta: Optional[float] = None
    undercut_decelerating: Optional[bool] = None
    post_low_trend: Optional[str] = None
    retests_held: Optional[int] = None
    hv_vs_peak: Optional[float] = None
    reclaim_streak: Optional[int] = None
    # 20-day mean of close x volume — the quality gate's liquidity input.
    dollar_volume: Optional[float] = None
    # ADR-0007 quality gate (#116). None = gate not run or unavailable;
    # quality_fails names each failed rule (e.g. "free cash flow").
    quality_pass: Optional[bool] = None
    quality_fails: list[str] = []
    # Fact columns (#117). The page sorts on these; ``score`` is the legacy
    # composite, shown for one quarter (remove ~2027-01-10).
    pct_off_high: Optional[float] = None  # % below the 52-week high, <= 0
    sector: Optional[str] = None  # from the quality gate's FMP profile
    earnings_date: Optional[str] = None  # next report, YYYY-MM-DD
    days_to_earnings: Optional[int] = None
    last_earnings_date: Optional[str] = None
    last_eps_actual: Optional[float] = None
    last_eps_estimate: Optional[float] = None
    eps_surprise_pct: Optional[float] = None  # > 0 beat, < 0 miss
    earnings_reaction_pct: Optional[float] = None  # signed move around last report
    earnings_reaction_vs_spy: Optional[float] = None  # same, minus SPY's move
    score: int  # legacy score
    signals: list[str]


class ScanExecuteResponse(BaseModel):
    """Response after starting a scan."""

    scan_id: str
    universe: str
    universe_size: int
    # Where the ticker list came from: static | screener | screener_cache |
    # screener_stale_cache | static_fallback (#118).
    universe_source: Optional[str] = None
    message: str


class ScanResultsResponse(BaseModel):
    """Response containing scan results."""

    scan_id: str
    universe: str
    total_analyzed: int
    results: list[ScanResultItem]
    scanned_at: datetime
    errors: int = 0  # Number of stocks that failed analysis
    warning: Optional[str] = None  # Warning message (e.g., rate limiting)
    partial: bool = False  # True while the scan is still running (issue #53)


# For historical scan storage (future: database models)
class ScanSnapshotCreate(BaseModel):
    """Create a scan snapshot record."""

    universe_name: str
    universe_size: int
    results_count: int
    filters_applied: Optional[dict] = None


class ScanSnapshotResponse(BaseModel):
    """Scan snapshot from history."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    universe_name: str
    universe_size: int
    results_count: int
    filters_applied: Optional[dict] = None
    scanned_at: datetime
