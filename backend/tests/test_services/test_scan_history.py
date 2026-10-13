"""scan_picks forward record + fundamentals_snapshots cache (ADR-0007)."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import get_session_factory
from app.main import app
from app.models.fundamentals import FundamentalsSnapshot
from app.models.scan_history import ScanPick
from app.routes import scanner as scanner_routes
from app.services.fundamentals import evaluate_snapshot, refresh_fundamentals
from app.services.market import get_market
from app.services.scan_history import record_scan_picks
from scripts.backtest.synthetic import make_statements


def _results(n):
    return [
        {"ticker": f"T{i:02d}", "price": 50.0 + i, "score": 100 - i, "setup": "Basing",
         "signals": ["x"], "rsi": float("nan") if i == 0 else 40.0}
        for i in range(n)
    ]


async def test_records_full_list_flagging_legacy_top_n(db_session):
    # #117: the whole shown list is recorded; the legacy top 20 is a flag.
    scan_id = str(uuid.uuid4())
    results = _results(30)
    results[3]["price"] = None  # unmeasurable: skipped but keeps its slot
    results[0]["sector"] = "Industrials"
    results[0]["eps_surprise_pct"] = 4.2
    try:
        n = await record_scan_picks(db_session, scan_id, "combined", results, top_n=20)
        assert n == 29
        rows = (await db_session.execute(
            select(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id)).order_by(ScanPick.rank)
        )).scalars().all()
        assert [r.rank for r in rows][:4] == [1, 2, 3, 5]
        assert rows[0].ticker == "T00" and rows[0].ranker == "scanner"
        assert rows[0].payload["signals"] == ["x"]
        assert rows[0].payload.get("rsi") is None  # NaN not written as JSON
        assert rows[0].payload["sector"] == "Industrials"
        assert rows[0].payload["eps_surprise_pct"] == 4.2
        flagged = [r.rank for r in rows if r.payload["legacy_top20"]]
        assert flagged == [1, 2, 3] + list(range(5, 21))
        assert all(r.payload["legacy_top20"] is False for r in rows if r.rank > 20)
    finally:
        await db_session.execute(delete(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id)))
        await db_session.commit()


class _ResultScanner:
    client = None

    async def run_scan(self, tickers, progress_callback=None, use_cache=True, universe=None,
                       results_out=None, quality_gate=None):
        return ("ignored", _results(25))


class _Market:
    scanner = _ResultScanner()


async def test_completed_scan_writes_its_picks(client, db_session):
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    app.dependency_overrides[get_market] = lambda: _Market()
    app.dependency_overrides[get_session_factory] = lambda: factory
    scanner_routes._active_scans.clear()
    res = await client.post("/api/scan/execute", json={"universe": "sp500_sample"})
    scan_id = res.json()["scan_id"]
    try:
        for _ in range(100):
            if scanner_routes._scan_tasks:
                await asyncio.sleep(0.02)
            else:
                break
        async with factory() as s:
            rows = (await s.execute(
                select(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id))
            )).scalars().all()
        assert len(rows) == 25
        assert {r.universe for r in rows} == {"sp500_sample"}
        assert sum(r.payload["legacy_top20"] for r in rows) == 20
    finally:
        async with factory() as s:
            await s.execute(delete(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id)))
            await s.commit()
        scanner_routes._active_scans.clear()


class _FakeFMP:
    def __init__(self, quality=True):
        rng = np.random.default_rng(3)
        days = pd.bdate_range("2023-01-01", "2026-09-30")
        self.inc, self.bal, self.cf = make_statements(rng, days, quality, shares=1e8)
        self.calls = 0

    async def get_quarterly_statements(self, kind, ticker, limit=4):
        self.calls += 1
        return {"income-statement": self.inc, "balance-sheet-statement": self.bal,
                "cash-flow-statement": self.cf}[kind][:limit]

    async def get_company_profile(self, ticker):
        return {"sector": "Industrials", "price": 100.0}


async def test_refresh_upserts_and_skips_fresh_rows(db_session):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    client = _FakeFMP()
    try:
        out = await refresh_fundamentals(db_session, client, ["ZZQA"], now=now)
        assert out["written"] == 1
        snap = (await db_session.execute(
            select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == "ZZQA")
        )).scalar_one()
        assert snap.quality_pass is True and snap.quality_fails == []
        assert len(snap.income) == 12
        ok, fails = evaluate_snapshot(snap, 100.0, 50e6)
        assert ok, fails

        calls = client.calls
        out = await refresh_fundamentals(db_session, client, ["ZZQA"], now=now + timedelta(days=1))
        assert out == {"requested": 1, "fresh": 1, "written": 0, "failed": 0}
        assert client.calls == calls

        bad = _FakeFMP(quality=False)
        await refresh_fundamentals(db_session, bad, ["ZZQA"], now=now + timedelta(days=8))
        db_session.expire_all()
        snap = (await db_session.execute(
            select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == "ZZQA")
        )).scalar_one()
        assert snap.quality_pass is False and "free cash flow" in snap.quality_fails
    finally:
        await db_session.execute(delete(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == "ZZQA"))
        await db_session.commit()
