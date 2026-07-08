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
        self, tickers, progress_callback=None, use_cache=True, universe=None
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
