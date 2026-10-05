"""
Fundamentals cache for the funnel's quality gate (ADR-0007).

Fetches the three quarterly statements per ticker from FMP and upserts the
raw lists into ``fundamentals_snapshots``. The gate itself is always
``funnel.compute_quality_metrics`` + ``passes_quality_gate`` over those raw
lists, so the cache never drifts from the backtest's definition.

Not yet called by any route or scheduled job: wiring a refresh job is a
Cloud Scheduler change and waits on the backtest verdict.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fundamentals import FundamentalsSnapshot
from app.services.fmp_client import FMPClient
from app.services.funnel import (
    FunnelConfig,
    compute_quality_metrics,
    passes_quality_gate,
    statement_available_date,
)

logger = logging.getLogger(__name__)

# 12 quarters: 8 for TTM-vs-prior-TTM revenue, plus slack for late filers.
STATEMENT_QUARTERS = 12
REFRESH_AFTER = timedelta(days=7)


def evaluate_snapshot(
    snap: FundamentalsSnapshot,
    price: float,
    dollar_volume: Optional[float],
    as_of: Optional[date] = None,
    cfg: FunnelConfig = FunnelConfig(),
) -> tuple[bool, list[str]]:
    """Quality-gate verdict for a cached snapshot at ``price``."""
    as_of = as_of or date.today()
    m = compute_quality_metrics(snap.income, snap.balance, snap.cashflow, as_of, price, cfg)
    return passes_quality_gate(m, snap.sector, price, dollar_volume, cfg)


async def fetch_statements(client: FMPClient, ticker: str) -> dict:
    inc, bal, cf, profile = await asyncio.gather(
        client.get_quarterly_statements("income-statement", ticker, STATEMENT_QUARTERS),
        client.get_quarterly_statements("balance-sheet-statement", ticker, STATEMENT_QUARTERS),
        client.get_quarterly_statements("cash-flow-statement", ticker, STATEMENT_QUARTERS),
        client.get_company_profile(ticker),
        return_exceptions=True,
    )
    clean = lambda v: v if isinstance(v, list) else []  # noqa: E731
    return {
        "income": clean(inc),
        "balance": clean(bal),
        "cashflow": clean(cf),
        "sector": profile.get("sector") if isinstance(profile, dict) else None,
        "price": profile.get("price") if isinstance(profile, dict) else None,
    }


async def upsert_snapshot(session: AsyncSession, ticker: str, data: dict, now: datetime) -> None:
    income = data["income"]
    latest = statement_available_date(income[0]) if income else None
    verdict, fails = None, []
    price = data.get("price")
    if price:
        # Liquidity is judged from bars at scan time; here only fundamentals.
        m = compute_quality_metrics(income, data["balance"], data["cashflow"], now.date() + timedelta(days=1), float(price))
        verdict, fails = passes_quality_gate(m, data["sector"], float(price), float("inf"))
    values = {
        "ticker": ticker,
        "sector": data["sector"],
        "income": income,
        "balance": data["balance"],
        "cashflow": data["cashflow"],
        "latest_filing_date": latest,
        "quality_pass": verdict,
        "quality_fails": fails,
        "fetched_at": now,
    }
    stmt = insert(FundamentalsSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fundamentals_snapshots_ticker",
        set_={**{k: stmt.excluded[k] for k in values if k != "ticker"}, "updated_at": now},
    )
    await session.execute(stmt)


async def refresh_fundamentals(
    session: AsyncSession,
    client: FMPClient,
    tickers: list[str],
    max_age: timedelta = REFRESH_AFTER,
    now: Optional[datetime] = None,
) -> dict:
    """Refetch snapshots older than ``max_age``. Returns counts."""
    now = now or datetime.now(timezone.utc)
    res = await session.execute(
        select(FundamentalsSnapshot.ticker, FundamentalsSnapshot.fetched_at).where(
            FundamentalsSnapshot.ticker.in_(tickers)
        )
    )
    fresh = {t for t, f in res.all() if now - f < max_age}
    stale = [t for t in tickers if t not in fresh]
    written = failed = 0
    for t in stale:
        data = await fetch_statements(client, t)
        if not data["income"]:
            failed += 1
            continue
        await upsert_snapshot(session, t, data, now)
        written += 1
    await session.commit()
    return {"requested": len(tickers), "fresh": len(fresh), "written": written, "failed": failed}
