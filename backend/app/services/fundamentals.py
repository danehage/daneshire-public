"""
Fundamentals cache for the funnel's quality gate (ADR-0007).

Fetches the three quarterly statements per ticker from FMP and upserts the
raw lists into ``fundamentals_snapshots``. The gate itself is always
``funnel.compute_quality_metrics`` + ``passes_quality_gate`` over those raw
lists, so the cache never drifts from the backtest's definition.

The live scan applies the gate through ``ScanQualityGate`` (#116), which
refreshes stale snapshots inline as Stage 2 reaches each ticker — no
scheduled job.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import pytz

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fundamentals import FundamentalsSnapshot
from app.services.fmp_client import FMPClient
from app.services.funnel import (
    FunnelConfig,
    QualityMetrics,
    compute_quality_metrics,
    passes_quality_gate,
    statement_available_date,
)
from app.services.quality_facts import compute_quality_facts

logger = logging.getLogger(__name__)

# 12 quarters: 8 for TTM-vs-prior-TTM revenue, plus slack for late filers.
STATEMENT_QUARTERS = 12
REFRESH_AFTER = timedelta(days=7)
# Snapshots read per query when preloading a scan; bounds memory, since
# each holds ~36 raw statements.
_PRELOAD_CHUNK = 100

_ET = pytz.timezone("America/New_York")


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


class ScanQualityGate:
    """The ADR-0007 quality gate applied to a live scan (#116).

    ``preload`` reads the cached snapshots for the Stage-1 survivors in a few
    bulk queries and reduces each to ``QualityMetrics`` straight away, so the
    raw statements never sit in memory for the whole universe. ``evaluate``
    then runs per ticker in Stage 2: a snapshot missing or older than
    ``max_age`` is refetched from FMP and upserted first. The verdict is
    always ``passes_quality_gate`` — no live-only thresholds.

    If the snapshot table can't be read the gate is ``available = False``
    and every verdict is ``None`` (unknown) rather than a failure: a DB
    outage must not empty the scanner. Missing *company* data still fails,
    as in the backtest.
    """

    def __init__(
        self,
        session_factory: Callable,
        client: FMPClient,
        cfg: FunnelConfig = FunnelConfig(),
        max_age: timedelta = REFRESH_AFTER,
        now: Optional[datetime] = None,
    ):
        self._session_factory = session_factory
        self._client = client
        self._cfg = cfg
        self._max_age = max_age
        self._now = now or datetime.now(timezone.utc)
        # Statements filed any time today are public by scan time.
        self._as_of = self._now.astimezone(_ET).date() + timedelta(days=1)
        self._metrics: dict[str, tuple[Optional[QualityMetrics], Optional[str]]] = {}
        self._facts: dict[str, dict] = {}
        self._fresh: set[str] = set()
        self.available = True
        self.refreshed = 0
        self.refresh_failed = 0

    def _reduce(self, ticker: str, income, balance, cashflow, sector, quote: dict) -> None:
        price = float(quote.get("price") or 0)
        mcap = quote.get("marketCap") or None
        m = compute_quality_metrics(
            income, balance, cashflow, self._as_of, price, self._cfg,
            market_cap=float(mcap) if mcap else None,
        )
        self._metrics[ticker] = (m, sector)
        self._facts[ticker] = compute_quality_facts(
            income, balance, self._as_of, sector, m, self._cfg
        )

    async def preload(self, quotes: dict[str, dict]) -> None:
        tickers = sorted(quotes)
        try:
            for i in range(0, len(tickers), _PRELOAD_CHUNK):
                chunk = tickers[i : i + _PRELOAD_CHUNK]
                async with self._session_factory() as session:
                    res = await session.execute(
                        select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker.in_(chunk))
                    )
                    for snap in res.scalars():
                        self._reduce(
                            snap.ticker, snap.income, snap.balance, snap.cashflow,
                            snap.sector, quotes[snap.ticker],
                        )
                        if self._now - snap.fetched_at < self._max_age:
                            self._fresh.add(snap.ticker)
        except Exception as e:
            logger.warning(f"Quality gate unavailable, scan rows left unjudged: {e}")
            self.available = False
            self._metrics.clear()
            self._facts.clear()
            self._fresh.clear()
            return
        logger.info(
            f"Quality gate: {len(self._fresh)} fresh snapshots, "
            f"{len(tickers) - len(self._fresh)} to refresh"
        )

    async def _refresh(self, ticker: str, quote: dict) -> None:
        data = await fetch_statements(self._client, ticker)
        if not data["income"]:
            # Keep a stale snapshot over nothing; with none, the gate fails
            # the ticker on "no fundamentals".
            self.refresh_failed += 1
            return
        try:
            async with self._session_factory() as session:
                await upsert_snapshot(session, ticker, data, self._now)
                await session.commit()
        except Exception as e:
            logger.warning(f"Quality gate: could not cache {ticker} fundamentals: {e}")
        self._reduce(
            ticker, data["income"], data["balance"], data["cashflow"], data["sector"], quote
        )
        self._fresh.add(ticker)
        self.refreshed += 1

    def sector(self, ticker: str) -> Optional[str]:
        """Sector from the ticker's snapshot (FMP profile), for the scanner's
        sector column (#117). Call after ``evaluate``; None if unknown."""
        return self._metrics.get(ticker, (None, None))[1]

    def facts(self, ticker: str) -> dict:
        """Quality and value fact columns for the row. Call after
        ``evaluate``; empty if the ticker has no snapshot."""
        return self._facts.get(ticker, {})

    async def evaluate(self, result: dict) -> tuple[Optional[bool], list[str]]:
        """(passed, failure reasons) for one scored scan row. ``passed`` is
        None when the gate is unavailable."""
        if not self.available:
            return None, []
        ticker = result["ticker"]
        quote = {"price": result.get("price"), "marketCap": result.get("market_cap")}
        if ticker not in self._fresh:
            try:
                await self._refresh(ticker, quote)
            except Exception as e:
                self.refresh_failed += 1
                logger.debug(f"Quality gate: refresh failed for {ticker}: {e}")
        m, sector = self._metrics.get(ticker, (None, None))
        return passes_quality_gate(
            m, sector, float(result.get("price") or 0), result.get("dollar_volume"), self._cfg
        )
