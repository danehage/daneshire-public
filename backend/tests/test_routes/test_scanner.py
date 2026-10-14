"""Route tests for the scanner.

Focus: ``POST /api/scan/execute`` must return immediately with a scan_id and
run the scan in the background, with progress reported over the SSE stream.
This guards against a regression to the old synchronous behaviour that timed
out (504) on large universes.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.routes import scanner as scanner_routes
from app.services.market import get_market
from app.main import app


class _FakeScanner:
    """Stands in for the real ScannerService.

    ``run_scan`` sleeps briefly to mimic real work, so the test can prove
    ``/execute`` returns *before* the scan finishes.
    """

    client = None  # the route hands it to ScanQualityGate

    def __init__(self):
        self.started = asyncio.Event()

    async def run_scan(
        self,
        tickers,
        progress_callback=None,
        use_cache=True,
        universe=None,
        results_out=None,
        quality_gate=None,
    ):
        self.started.set()
        if progress_callback:
            await progress_callback(
                {
                    "type": "progress",
                    "current": len(tickers),
                    "total": len(tickers),
                    "found": 0,
                    "errors": 0,
                }
            )
        # Yield control so /execute can return before we "complete".
        await asyncio.sleep(0.05)
        return ("scan-x", [])


class _FakeMarket:
    def __init__(self):
        self.scanner = _FakeScanner()


@pytest.mark.asyncio
async def test_execute_returns_immediately_and_streams_complete(client):
    fake = _FakeMarket()
    app.dependency_overrides[get_market] = lambda: fake

    # /execute returns right away with a scan_id, not after the scan finishes.
    res = await client.post("/api/scan/execute", json={"universe": "sp500_sample"})
    assert res.status_code == 200
    body = res.json()
    scan_id = body["scan_id"]
    assert scan_id
    assert body["message"].startswith("Scan started")
    assert body["universe_size"] > 0

    # The SSE stream reports completion once the background task finishes.
    saw_complete = False
    async with client.stream("GET", f"/api/scan/{scan_id}/stream") as stream:
        async for line in stream.aiter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: ") :])
            if event["type"] == "complete":
                saw_complete = True
                break
    assert saw_complete

    # Results are now retrievable.
    results = await client.get(f"/api/scan/{scan_id}/results")
    assert results.status_code == 200
    assert results.json()["total_analyzed"] == 0
    assert results.json()["partial"] is False


def _row(ticker: str, score: int) -> dict:
    """Minimal dict satisfying ScanResultItem's required fields."""
    return {
        "ticker": ticker,
        "price": 50.0,
        "market_cap": 5_000_000_000,
        "volume": 1_000_000,
        "avg_volume": 900_000,
        "volume_pace": 1.0,
        "volume_pace_reliable": False,
        "trend": "Consolidation",
        "range_position": 40.0,
        "high_52w": 80.0,
        "low_52w": 30.0,
        "support": 45.0,
        "resistance": 55.0,
        "support_type": "recent_low_20d",
        "resistance_type": "recent_high_20d",
        "volume_ratio": 1.0,
        "rsi": 50.0,
        "momentum_10d": 0.0,
        "score": score,
        "signals": [],
    }


class _PausableScanner:
    """Appends two scored rows to ``results_out``, then blocks until told to
    finish — so the test can hit /results mid-scan (issue #53)."""

    client = None  # the route hands it to ScanQualityGate

    def __init__(self):
        self.first_batch_done = asyncio.Event()
        self.proceed = asyncio.Event()

    async def run_scan(
        self,
        tickers,
        progress_callback=None,
        use_cache=True,
        universe=None,
        results_out=None,
        quality_gate=None,
    ):
        results_out.append(_row("AAA", 10))
        results_out.append(_row("BBB", 70))
        self.first_batch_done.set()
        await self.proceed.wait()
        results_out.append(_row("CCC", 40))
        return ("scan-y", sorted(results_out, key=lambda r: r["score"], reverse=True))


@pytest.mark.asyncio
async def test_results_returns_scored_partials_while_running(client):
    fake = _FakeMarket()
    fake.scanner = _PausableScanner()
    app.dependency_overrides[get_market] = lambda: fake

    res = await client.post("/api/scan/execute", json={"universe": "sp500_sample"})
    scan_id = res.json()["scan_id"]

    await asyncio.wait_for(fake.scanner.first_batch_done.wait(), timeout=2)

    # Mid-scan: 200 with a score-sorted partial snapshot, not 202.
    partial = await client.get(f"/api/scan/{scan_id}/results")
    assert partial.status_code == 200
    body = partial.json()
    assert body["partial"] is True
    assert [r["ticker"] for r in body["results"]] == ["BBB", "AAA"]

    # Let the scan finish; the final payload includes the late row and
    # drops the partial flag.
    fake.scanner.proceed.set()
    for _ in range(40):
        final = await client.get(f"/api/scan/{scan_id}/results")
        if final.json()["partial"] is False:
            break
        await asyncio.sleep(0.05)
    body = final.json()
    assert body["partial"] is False
    assert [r["ticker"] for r in body["results"]] == ["BBB", "CCC", "AAA"]


# --- Scan state bounds -----------------------------------------------------
#
# Each scan_state retains the full scored result set for its universe
# (~900 rows). _active_scans used to grow for the life of the process, so
# repeated scans exhausted the 512Mi Cloud Run instance; and nothing capped
# how many scans could run at once against a paid upstream API.


@pytest.mark.asyncio
async def test_second_concurrent_scan_is_refused(client):
    fake = _FakeMarket()
    app.dependency_overrides[get_market] = lambda: fake
    scanner_routes._active_scans.clear()
    try:
        first = await client.post("/api/scan/execute", json={"universe": "sp500_sample"})
        assert first.status_code == 200

        second = await client.post("/api/scan/execute", json={"universe": "sp500_sample"})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        scanner_routes._active_scans.clear()


def test_prune_drops_scans_that_aged_out():
    scanner_routes._active_scans.clear()
    stale = datetime.utcnow() - timedelta(
        seconds=scanner_routes._SCAN_RETENTION_SECONDS + 60
    )
    scanner_routes._active_scans["old"] = {
        "status": "complete",
        "started_at": stale,
        "completed_at": stale,
    }
    scanner_routes._active_scans["fresh"] = {
        "status": "complete",
        "started_at": datetime.utcnow(),
        "completed_at": datetime.utcnow(),
    }

    scanner_routes._prune_scans()

    assert "old" not in scanner_routes._active_scans
    assert "fresh" in scanner_routes._active_scans
    scanner_routes._active_scans.clear()


def test_prune_enforces_the_count_cap():
    scanner_routes._active_scans.clear()
    now = datetime.utcnow()
    for i in range(scanner_routes._MAX_RETAINED_SCANS + 5):
        scanner_routes._active_scans[f"scan-{i}"] = {
            "status": "complete",
            # Ascending timestamps: scan-0 is the oldest.
            "started_at": now + timedelta(seconds=i),
            "completed_at": now + timedelta(seconds=i),
        }

    scanner_routes._prune_scans()

    assert len(scanner_routes._active_scans) == scanner_routes._MAX_RETAINED_SCANS
    # The oldest five went; the newest survived.
    assert "scan-0" not in scanner_routes._active_scans
    assert f"scan-{scanner_routes._MAX_RETAINED_SCANS + 4}" in scanner_routes._active_scans
    scanner_routes._active_scans.clear()


def test_prune_never_drops_a_running_scan():
    scanner_routes._active_scans.clear()
    stale = datetime.utcnow() - timedelta(
        seconds=scanner_routes._SCAN_RETENTION_SECONDS + 60
    )
    scanner_routes._active_scans["running"] = {
        "status": "running",
        "started_at": stale,
    }

    scanner_routes._prune_scans()

    assert "running" in scanner_routes._active_scans
    scanner_routes._active_scans.clear()


# --- Screener-built ``combined`` universe (#118) ---------------------------


class _RecordingScanner(_FakeScanner):
    """Records what run_scan was asked to scan; ``client`` serves the
    screener."""

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.calls: list[dict] = []

    async def run_scan(self, tickers, universe=None, **kwargs):
        self.calls.append({"tickers": list(tickers), "universe": universe})
        return ("scan-z", [])


class _ScreenerClient:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    async def get_company_screener(self, params):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HTTP 503")
        return list(self.rows)


def _screener_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "companyName": f"{symbol} Corp.",
        "marketCap": 9e9,
        "price": 100.0,
        "avgVolume": 1_000_000,
        "exchangeShortName": "NYSE",
        "country": "US",
        "isEtf": False,
        "isFund": False,
        "isActivelyTrading": True,
    }


@pytest.fixture
def temp_universe_cache(monkeypatch, tmp_path):
    from app.services import screener_universe
    from app.services.screener_universe import UniverseCache

    cache = UniverseCache(str(tmp_path / "universe_screener.json"))
    monkeypatch.setattr(scanner_routes, "universe_cache", cache)
    monkeypatch.setattr(screener_universe, "MIN_UNIVERSE_SIZE", 1)
    scanner_routes._active_scans.clear()
    yield cache
    scanner_routes._active_scans.clear()


async def _wait_for_run(scanner):
    for _ in range(40):
        if scanner.calls:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("scan never started")


@pytest.mark.asyncio
async def test_combined_scans_the_screener_build(client, temp_universe_cache):
    screener = _ScreenerClient([_screener_row("AAA"), _screener_row("BBB")])
    fake = _FakeMarket()
    fake.scanner = _RecordingScanner(screener)
    app.dependency_overrides[get_market] = lambda: fake

    # No build on this instance yet: size unknown, and listing doesn't fetch.
    listing = await client.get("/api/scan/universes")
    combined = next(u for u in listing.json() if u["name"] == "combined")
    assert combined["size"] is None
    assert screener.calls == 0

    res = await client.post("/api/scan/execute", json={"universe": "combined"})
    assert res.status_code == 200
    body = res.json()
    assert body["universe"] == "combined"
    assert body["universe_size"] == 2
    assert body["universe_source"] == "screener"
    assert screener.calls == 2  # US-domicile + every-domicile queries

    await _wait_for_run(fake.scanner)
    # Stage-1 cache key / scan_picks label keeps screener scans apart from
    # the static list's.
    assert fake.scanner.calls == [{"tickers": ["AAA", "BBB"], "universe": "combined_screener"}]

    listing = await client.get("/api/scan/universes")
    combined = next(u for u in listing.json() if u["name"] == "combined")
    assert combined["size"] == 2


@pytest.mark.asyncio
async def test_combined_falls_back_to_static_list_when_screener_fails(client, temp_universe_cache):
    from app.services.universes import get_combined_universe

    fake = _FakeMarket()
    fake.scanner = _RecordingScanner(_ScreenerClient(fail=True))
    app.dependency_overrides[get_market] = lambda: fake

    res = await client.post("/api/scan/execute", json={"universe": "combined"})
    assert res.status_code == 200
    body = res.json()
    assert body["universe_source"] == "static_fallback"
    assert body["universe_size"] == len(get_combined_universe())
    assert "static S&P 500 + 400" in body["message"]

    await _wait_for_run(fake.scanner)
    assert fake.scanner.calls[0]["universe"] == "combined"


@pytest.mark.asyncio
async def test_unknown_universe_is_400_without_calling_fmp(client, temp_universe_cache):
    screener = _ScreenerClient([_screener_row("AAA")])
    fake = _FakeMarket()
    fake.scanner = _RecordingScanner(screener)
    app.dependency_overrides[get_market] = lambda: fake

    res = await client.post("/api/scan/execute", json={"universe": "robinhood"})
    assert res.status_code == 400
    assert screener.calls == 0


# --- GET /api/scan/latest (#126) --------------------------------------------


@pytest.mark.asyncio
async def test_latest_is_404_before_any_scan_is_saved(client, monkeypatch):
    async def nothing_saved(session):
        return None

    monkeypatch.setattr(scanner_routes, "load_latest_scan", nothing_saved)
    res = await client.get("/api/scan/latest")
    assert res.status_code == 404
    assert res.json() == {"detail": "No saved scan yet"}


@pytest.mark.asyncio
async def test_latest_is_not_shadowed_by_scan_id_routes(client, monkeypatch):
    """``latest`` must reach its own route, not be read as a scan_id."""

    async def one_row(session):
        return {
            "scan_id": "00000000-0000-0000-0000-000000000126",
            "universe": "combined_screener",
            "scanned_at": datetime(2026, 10, 13, 21, 0),
            # A pre-#126 row: only the fields recorded back then.
            "results": [{"ticker": "AAA", "price": 12.5, "score": 70, "signals": ["x"]}],
        }

    monkeypatch.setattr(scanner_routes, "load_latest_scan", one_row)
    res = await client.get("/api/scan/latest")
    assert res.status_code == 200
    body = res.json()
    assert body["saved"] is True
    assert body["universe"] == "combined_screener"
    assert body["total_analyzed"] == 1
    row = body["results"][0]
    assert row["ticker"] == "AAA" and row["price"] == 12.5
    # Fields the old record didn't keep come back blank, not as errors.
    assert row["support"] is None and row["volume_pace"] is None
