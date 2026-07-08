"""Parsing tests for MarketData._fetch_ticker_context (PR #52 / issue #50).

The fake client returns payloads shaped like FMP's /stable/ API (recorded
via curl 2026-07-08). These exist because the original PR parsed the client
returns as if they were still list-wrapped — every profile/ratio field came
back None and no test caught it (the route tests fake ticker_context()
one level above the parsing).
"""

from __future__ import annotations

import pytest

from app.services.market import MarketData, TickerNotFound


class _FakeFMP:
    """Stable-API-shaped payloads, already unwrapped by the client methods."""

    async def get_company_profile(self, ticker: str):
        return {
            "symbol": ticker,
            "price": 310.66,
            "range": "201.5-317.4",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "description": "Apple Inc. designs consumer electronics.",
        }

    async def get_ratios_ttm(self, ticker: str):
        # /stable/ratios-ttm — margin + D/E live here now, NOT in
        # key-metrics-ttm, and D/E is debtToEquityRatioTTM.
        return {
            "netProfitMarginTTM": 0.2431,
            "debtToEquityRatioTTM": 1.54,
        }

    async def get_income_statements(self, ticker: str, limit: int = 4):
        # Most recent first, as FMP returns them. No netIncomeRatio field
        # on /stable/ — per-quarter margin must be computed.
        return [
            {
                "date": "2026-03-28",
                "revenue": 111_184_000_000,
                "eps": 1.65,
                "netIncome": 26_000_000_000,
            },
            {
                "date": "2025-12-27",
                "revenue": 124_300_000_000,
                "eps": 2.40,
                "netIncome": 36_330_000_000,
            },
        ]

    async def get_stock_news(self, ticker: str, limit: int = 5):
        return [
            {
                "title": "Apple signs $30B AVGO deal",
                "url": "https://example.com/avgo",
                "publishedDate": "2026-07-08 08:30:27",
                "site": "schwabnetwork.com",
                "publisher": "Schwab Network",
                "text": "…",
            }
        ]

    async def get_earnings_date(self, ticker: str):
        return {"date": "2026-07-30", "days_until": 22}


def _market(client) -> MarketData:
    return MarketData(client=client, scanner=None)


@pytest.mark.asyncio
async def test_fetch_ticker_context_parses_stable_payloads():
    ctx = await _market(_FakeFMP())._fetch_ticker_context("AAPL")

    # Profile — the fields the original PR silently dropped
    assert ctx["company_name"] == "Apple Inc."
    assert ctx["sector"] == "Technology"
    assert ctx["description"] == "Apple Inc. designs consumer electronics."
    assert ctx["current_price"] == pytest.approx(310.66)
    assert ctx["high_52w"] == pytest.approx(317.4)
    assert ctx["drawdown_from_52w_high_pct"] == pytest.approx(2.1, abs=0.05)

    # Ratios
    assert ctx["net_margin_ttm"] == pytest.approx(24.3)
    assert ctx["debt_to_equity"] == pytest.approx(1.54)

    # Income statements: chronological for display, margin computed
    quarters = ctx["quarterly_financials"]
    assert [q["quarter"] for q in quarters] == ["Q4 2025", "Q1 2026"]
    assert quarters[1]["revenue"] == 111_184_000_000
    assert quarters[1]["net_margin"] == pytest.approx(23.4, abs=0.05)
    assert quarters[1]["revenue_pct_change"] == pytest.approx(-10.6, abs=0.05)
    assert quarters[1]["eps_pct_change"] == pytest.approx(-31.25, abs=0.1)

    # News + earnings
    assert ctx["headlines"][0]["title"] == "Apple signs $30B AVGO deal"
    assert ctx["headlines"][0]["source"] == "schwabnetwork.com"
    assert ctx["headlines"][0]["published_at"] == "2026-07-08 08:30:27"
    assert ctx["next_earnings_date"] == "2026-07-30"
    assert ctx["days_to_earnings"] == 22


@pytest.mark.asyncio
async def test_fetch_ticker_context_unknown_symbol_raises():
    class _NoProfile(_FakeFMP):
        async def get_company_profile(self, ticker: str):
            return None

    with pytest.raises(TickerNotFound):
        await _market(_NoProfile())._fetch_ticker_context("ZZZZZZZ")


@pytest.mark.asyncio
async def test_fetch_ticker_context_survives_partial_failures():
    """Profile alone is enough; every other upstream failing → None fields."""

    class _ProfileOnly(_FakeFMP):
        async def get_ratios_ttm(self, ticker: str):
            raise RuntimeError("FMP down")

        async def get_income_statements(self, ticker: str, limit: int = 4):
            return None

        async def get_stock_news(self, ticker: str, limit: int = 5):
            raise RuntimeError("FMP down")

        async def get_earnings_date(self, ticker: str):
            return None

    ctx = await _market(_ProfileOnly())._fetch_ticker_context("AAPL")
    assert ctx["company_name"] == "Apple Inc."
    assert ctx["net_margin_ttm"] is None
    assert ctx["debt_to_equity"] is None
    assert ctx["quarterly_financials"] == []
    assert ctx["headlines"] == []
    assert ctx["next_earnings_date"] is None
