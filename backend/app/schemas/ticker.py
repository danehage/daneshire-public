from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class QuarterlyFinancials(BaseModel):
    quarter: str
    revenue: Optional[int] = None
    revenue_pct_change: Optional[float] = None
    eps: Optional[float] = None
    eps_pct_change: Optional[float] = None
    net_margin: Optional[float] = None


class Headline(BaseModel):
    title: str
    url: str
    published_at: str
    source: str


class TickerContextResponse(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    description: Optional[str] = None
    current_price: Optional[float] = None
    high_52w: Optional[float] = None
    drawdown_from_52w_high_pct: Optional[float] = None
    quarterly_financials: list[QuarterlyFinancials] = []
    net_margin_ttm: Optional[float] = None
    debt_to_equity: Optional[float] = None
    next_earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    headlines: list[Headline] = []


class TickerNarrativeResponse(BaseModel):
    ticker: str
    narrative: Optional[str] = None
    narrative_available: bool
