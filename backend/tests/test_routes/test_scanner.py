"""Route tests for the scanner.

Focus: ``POST /api/scan/execute`` must return immediately with a scan_id and
run the scan in the background, with progress reported over the SSE stream.
This guards against a regression to the old synchronous behaviour that timed
out (504) on large universes.
"""

import asyncio
import json

import pytest

from app.services.market import get_market
from app.main import app


class _FakeScanner:
    """Stands in for the real ScannerService.

    ``run_scan`` sleeps briefly to mimic real work, so the test can prove
    ``/execute`` returns *before* the scan finishes.
    """

    def __init__(self):
        self.started = asyncio.Event()

    async def run_scan(
        self,
        tickers,
        progress_callback=None,
        use_cache=True,
        universe=None,
        results_out=None,
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
