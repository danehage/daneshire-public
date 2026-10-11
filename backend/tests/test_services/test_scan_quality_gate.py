"""ADR-0007 quality gate in the live scan (#116).

Hand-built statements so each sector rule can be flipped on its own. FMP is
always a fake; the snapshot cache is the real ``fundamentals_snapshots``
table on the test database.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.fundamentals import FundamentalsSnapshot
from app.services.fundamentals import ScanQualityGate
from app.services.scan_history import record_scan_picks
from app.services.scanner import StockScanner

NOW = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
PRICE = 100.0
SHARES = 1e8  # market cap $10B at PRICE
LIQUID = 50e6
TICKERS = ("ZQG1", "ZQG2", "ZQG3")


def _quarter_ends(n=8):
    """Newest first: 2026-06-30, 2026-03-31, ... (all filed before NOW)."""
    ends, y, q = [], 2026, 2
    for _ in range(n):
        month = q * 3
        day = 30 if month in (6, 9) else 31
        ends.append(date(y, month, day))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return ends


def make_statements(fcf=1.2e8, net_income=1e8, net_debt=4e8, distressed=False):
    """Eight quarters of a healthy $4B-revenue company; knobs break one rule.

    Baseline: revenue +2%/q, net debt / EBITDA 0.5, Altman Z ~5.
    ``distressed`` sinks Altman Z below 1.8 without touching leverage.
    """
    inc, bal, cf = [], [], []
    for i, end in enumerate(_quarter_ends()):
        filed = (end + timedelta(days=40)).isoformat()
        base = {"date": end.isoformat(), "filingDate": filed}
        inc.append({**base, "revenue": 1e9 * (1.02 ** -i), "netIncome": net_income,
                    "ebitda": 2e8, "operatingIncome": 1.5e8,
                    "weightedAverageShsOut": SHARES, "weightedAverageShsOutDil": SHARES})
        cf.append({**base, "freeCashFlow": fcf})
        bal.append({**base, "netDebt": net_debt, "totalAssets": 5e9,
                    "totalLiabilities": 4.9e9 if distressed else 2e9,
                    "totalCurrentAssets": 5e8 if distressed else 1.5e9,
                    "totalCurrentLiabilities": 2e9 if distressed else 8e8,
                    "retainedEarnings": -3e9 if distressed else 2e9})
    return inc, bal, cf


class FakeFMP:
    """Serves one company's statements per ticker; counts requests."""

    def __init__(self, companies: dict[str, tuple[str, tuple]]):
        self.companies = companies  # ticker -> (sector, (inc, bal, cf))
        self.calls = 0

    async def get_quarterly_statements(self, kind, ticker, limit=4):
        self.calls += 1
        if ticker not in self.companies:
            return []
        inc, bal, cf = self.companies[ticker][1]
        return {"income-statement": inc, "balance-sheet-statement": bal,
                "cash-flow-statement": cf}[kind][:limit]

    async def get_company_profile(self, ticker):
        self.calls += 1
        if ticker not in self.companies:
            return None
        return {"sector": self.companies[ticker][0], "price": PRICE}


def _row(ticker, price=PRICE, dollar_volume=LIQUID, score=10):
    return {"ticker": ticker, "price": price, "market_cap": int(price * SHARES),
            "dollar_volume": dollar_volume, "score": score}


@pytest.fixture
async def factory(db_session):
    f = async_sessionmaker(db_session.bind, expire_on_commit=False)
    yield f
    async with f() as s:
        await s.execute(delete(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker.in_(TICKERS)))
        await s.commit()


async def _judge(factory, sector, stmts, **row):
    fmp = FakeFMP({"ZQG1": (sector, stmts)})
    gate = ScanQualityGate(factory, fmp, now=NOW)
    r = _row("ZQG1", **row)
    await gate.preload({"ZQG1": {"price": r["price"], "marketCap": r["market_cap"]}})
    return await gate.evaluate(r)


# ---- sector rules ---------------------------------------------------------

CASES = [
    # sector, statement knobs, expected failure reasons
    ("Industrials", {}, []),
    ("Industrials", {"fcf": -1e7}, ["free cash flow"]),
    ("Industrials", {"net_debt": 4e9}, ["leverage"]),
    ("Industrials", {"distressed": True}, ["altman z"]),
    # Financials / REITs: net income replaces FCF; no leverage, no Altman Z.
    ("Financial Services", {}, []),
    ("Financial Services", {"fcf": -1e7}, []),
    ("Financial Services", {"net_income": -1e7}, ["net income"]),
    ("Financial Services", {"net_debt": 4e9, "distressed": True}, []),
    ("Real Estate", {"fcf": -1e7}, []),
    ("Real Estate", {"net_income": -1e7}, ["net income"]),
    ("Real Estate", {"net_debt": 4e9, "distressed": True}, []),
    # Utilities: judged on FCF, but no leverage or Altman Z test.
    ("Utilities", {}, []),
    ("Utilities", {"fcf": -1e7}, ["free cash flow"]),
    ("Utilities", {"net_debt": 4e9, "distressed": True}, []),
]


@pytest.mark.parametrize("sector,knobs,expected", CASES)
async def test_sector_rules(factory, sector, knobs, expected):
    passed, fails = await _judge(factory, sector, make_statements(**knobs))
    assert fails == expected
    assert passed is (not expected)


async def test_illiquid_name_fails_on_liquidity(factory):
    passed, fails = await _judge(factory, "Industrials", make_statements(), dollar_volume=5e6)
    assert passed is False and fails == ["liquidity"]


async def test_missing_fundamentals_fail(factory):
    gate = ScanQualityGate(factory, FakeFMP({}), now=NOW)
    await gate.preload({"ZQG1": {"price": PRICE, "marketCap": 1e10}})
    passed, fails = await gate.evaluate(_row("ZQG1"))
    assert passed is False and fails == ["no fundamentals"]
    assert gate.refresh_failed == 1


# ---- snapshot cache -------------------------------------------------------


async def test_first_scan_fetches_and_caches_then_reuses_for_a_week(factory):
    fmp = FakeFMP({"ZQG1": ("Industrials", make_statements())})
    quotes = {"ZQG1": {"price": PRICE, "marketCap": 1e10}}

    gate = ScanQualityGate(factory, fmp, now=NOW)
    await gate.preload(quotes)
    assert (await gate.evaluate(_row("ZQG1")))[0] is True
    assert fmp.calls == 4  # three statements + profile
    async with factory() as s:
        snap = (await s.execute(
            select(FundamentalsSnapshot).where(FundamentalsSnapshot.ticker == "ZQG1")
        )).scalar_one()
    assert snap.sector == "Industrials" and len(snap.income) == 8
    assert snap.fetched_at == NOW

    # Six days on: served from the cache, no FMP calls.
    gate = ScanQualityGate(factory, fmp, now=NOW + timedelta(days=6))
    await gate.preload(quotes)
    assert (await gate.evaluate(_row("ZQG1")))[0] is True
    assert fmp.calls == 4

    # Past a week: refetched — and the new filing flips the verdict.
    fmp.companies["ZQG1"] = ("Industrials", make_statements(fcf=-1e7))
    gate = ScanQualityGate(factory, fmp, now=NOW + timedelta(days=8))
    await gate.preload(quotes)
    assert await gate.evaluate(_row("ZQG1")) == (False, ["free cash flow"])
    assert fmp.calls == 8 and gate.refreshed == 1


async def test_stale_snapshot_is_used_when_refetch_fails(factory):
    fmp = FakeFMP({"ZQG1": ("Industrials", make_statements())})
    quotes = {"ZQG1": {"price": PRICE, "marketCap": 1e10}}
    gate = ScanQualityGate(factory, fmp, now=NOW)
    await gate.preload(quotes)
    await gate.evaluate(_row("ZQG1"))

    fmp.companies.clear()  # FMP now returns nothing
    gate = ScanQualityGate(factory, fmp, now=NOW + timedelta(days=30))
    await gate.preload(quotes)
    assert await gate.evaluate(_row("ZQG1")) == (True, [])
    assert gate.refresh_failed == 1


async def test_db_outage_leaves_rows_unjudged():
    def broken_factory():
        raise ConnectionError("db down")

    fmp = FakeFMP({"ZQG1": ("Industrials", make_statements())})
    gate = ScanQualityGate(broken_factory, fmp, now=NOW)
    await gate.preload({"ZQG1": {"price": PRICE, "marketCap": 1e10}})
    assert gate.available is False
    assert await gate.evaluate(_row("ZQG1")) == (None, [])
    assert fmp.calls == 0


# ---- scanner + forward record ---------------------------------------------


class _ScanClient:
    async def get_batch_quotes(self, tickers, max_concurrent=10):
        return {t: {"price": PRICE, "volume": 1_000_000, "marketCap": 1e10} for t in tickers}


async def test_scan_rows_carry_the_verdict(tmp_path, factory):
    fmp = FakeFMP({
        "ZQG1": ("Industrials", make_statements()),
        "ZQG2": ("Industrials", make_statements(net_debt=4e9)),
    })
    scanner = StockScanner(cache_dir=str(tmp_path), client=_ScanClient())

    async def analyze(ticker, quote):
        return _row(ticker) | {"trend": "Uptrend", "signals": []}

    scanner._process_ticker = analyze
    scanner._score_opportunity = lambda r: (10, [], None)

    _, results = await scanner.run_scan(
        ["ZQG1", "ZQG2", "ZQG3"], use_cache=False, quality_gate=ScanQualityGate(factory, fmp, now=NOW)
    )
    by = {r["ticker"]: (r["quality_pass"], r["quality_fails"]) for r in results}
    assert by == {
        "ZQG1": (True, []),
        "ZQG2": (False, ["leverage"]),
        "ZQG3": (False, ["no fundamentals"]),
    }


async def test_forward_record_skips_gate_failures(db_session):
    import uuid
    from app.models.scan_history import ScanPick

    scan_id = str(uuid.uuid4())
    results = [
        _row("ZQG1", score=90) | {"quality_pass": False, "quality_fails": ["leverage"]},
        _row("ZQG2", score=80) | {"quality_pass": True, "quality_fails": []},
        _row("ZQG3", score=70) | {"quality_pass": None, "quality_fails": []},
    ]
    try:
        assert await record_scan_picks(db_session, scan_id, "combined", results) == 2
        rows = (await db_session.execute(
            select(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id)).order_by(ScanPick.rank)
        )).scalars().all()
        assert [(r.ticker, r.rank) for r in rows] == [("ZQG2", 1), ("ZQG3", 2)]
        assert rows[0].payload["quality_pass"] is True
    finally:
        await db_session.execute(delete(ScanPick).where(ScanPick.scan_id == uuid.UUID(scan_id)))
        await db_session.commit()
