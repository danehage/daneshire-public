"""
Scanner API routes with SSE streaming support.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.scan import (
    ScanRequest,
    ScanExecuteResponse,
    ScanResultItem,
    ScanResultsResponse,
    UniverseInfo,
)
from app.schemas.tickers import normalize_ticker
from app.services import UNIVERSES, get_universe
from app.services.market import MarketData, TickerNotFound, get_market

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scan", tags=["scanner"])

# In-memory storage for active scans (replace with Redis/DB for production).
#
# Every entry holds the full scored result set for its universe (~900 rows),
# so this dict is bounded on two axes: entries are dropped once they age out
# of _SCAN_RETENTION_SECONDS, and the newest _MAX_RETAINED_SCANS are kept
# regardless. Without this the dict grew for the life of the process and
# repeated scans exhausted the 512Mi Cloud Run instance.
_active_scans: dict[str, dict] = {}

# Strong references to in-flight background scan tasks. asyncio only holds a
# weak reference to tasks, so without this they could be garbage-collected
# mid-scan. Tasks remove themselves on completion.
_scan_tasks: set[asyncio.Task] = set()

# Retention for finished scans, and the cap on how many are kept at once.
_SCAN_RETENTION_SECONDS = 3600
_MAX_RETAINED_SCANS = 10

# A scan holds a CPU-always-allocated worker for minutes and fans out to a
# paid upstream API. One at a time, per instance.
_MAX_CONCURRENT_SCANS = 1


def _running_scan_count() -> int:
    return sum(1 for s in _active_scans.values() if s["status"] == "running")


def _prune_scans() -> None:
    """Drop finished scans that have aged out, then enforce the count cap.

    Running scans are never pruned.
    """
    now = datetime.utcnow()
    finished = [
        (scan_id, state)
        for scan_id, state in _active_scans.items()
        if state["status"] != "running"
    ]

    for scan_id, state in finished:
        completed_at = state.get("completed_at") or state["started_at"]
        if (now - completed_at).total_seconds() > _SCAN_RETENTION_SECONDS:
            _active_scans.pop(scan_id, None)

    # Still too many? Drop the oldest finished scans until we are under cap.
    surplus = len(_active_scans) - _MAX_RETAINED_SCANS
    if surplus > 0:
        oldest_first = sorted(
            (
                (state.get("completed_at") or state["started_at"], scan_id)
                for scan_id, state in _active_scans.items()
                if state["status"] != "running"
            ),
            key=lambda pair: pair[0],
        )
        for _, scan_id in oldest_first[:surplus]:
            _active_scans.pop(scan_id, None)


@router.get("/universes", response_model=list[UniverseInfo])
async def list_universes():
    """List available stock universes with their sizes."""
    descriptions = {
        "combined": "S&P 500 + S&P 400 — the complete picture (~900 stocks)",
        "sp500": "Full S&P 500 constituents (~500 stocks)",
        "sp400": "S&P MidCap 400 — quality mid-caps (~400 stocks)",
        "sp500_sample": "100 representative large cap S&P 500 stocks",
    }

    return [
        UniverseInfo(
            name=name,
            size=len(tickers),
            description=descriptions.get(name, ""),
        )
        for name, tickers in UNIVERSES.items()
    ]


@router.post("/execute", response_model=ScanExecuteResponse)
async def execute_scan(
    request: ScanRequest,
    market: MarketData = Depends(get_market),
):
    """
    Start a new scan and return its scan_id immediately.

    The scan runs as a background task; the frontend tracks progress via the
    SSE ``/{scan_id}/stream`` endpoint and fetches results when complete. This
    avoids the 504 that a synchronous scan hit on large universes (Cloud Run's
    request timeout). Requires CPU-always-allocated on Cloud Run so the task
    keeps making progress after this request returns (see cloudbuild.yaml).
    """
    tickers = get_universe(request.universe)
    if not tickers:
        raise HTTPException(status_code=400, detail=f"Unknown universe: {request.universe}")

    _prune_scans()

    if _running_scan_count() >= _MAX_CONCURRENT_SCANS:
        raise HTTPException(
            status_code=409,
            detail=(
                "A scan is already running. Wait for it to finish before "
                "starting another."
            ),
        )

    # Delegate to the shared scanner so the FMP throttle is process-wide.
    scanner = market.scanner

    # Generate scan_id upfront
    import uuid
    temp_scan_id = str(uuid.uuid4())

    # Live results list, shared with the scanner: Stage 2 appends each
    # scored ticker as it completes, so /results can serve partials while
    # the scan runs (issue #53).
    live_results: list[dict] = []

    # Initialize scan state
    scan_state = {
        "status": "running",
        "universe": request.universe,
        "universe_size": len(tickers),
        "started_at": datetime.utcnow(),
        "progress": {"current": 0, "total": 0, "found": 0, "errors": 0},
        "results": live_results,
        "errors": 0,
        "scan_id": temp_scan_id,
        "temp_id": temp_scan_id,
    }
    _active_scans[temp_scan_id] = scan_state

    async def progress_callback(event: dict):
        """Update scan state with progress."""
        if event["type"] == "progress":
            scan_state["progress"] = {
                "current": event["current"],
                "total": event["total"],
                "found": event["found"],
                "errors": event.get("errors", 0),
            }
            scan_state["errors"] = event.get("errors", 0)
        elif event["type"] == "complete":
            scan_state["status"] = "complete"

    async def run_scan_task():
        """Run the scan to completion, updating shared scan_state as it goes."""
        try:
            _, results = await scanner.run_scan(
                tickers,
                progress_callback=progress_callback,
                use_cache=request.use_cache,
                universe=request.universe,
                results_out=live_results,
            )
            scan_state["results"] = results
            scan_state["status"] = "complete"
            scan_state["completed_at"] = datetime.utcnow()
        except Exception as e:
            logger.error(f"Scan {temp_scan_id} failed: {e}", exc_info=True)
            scan_state["status"] = "error"
            scan_state["error"] = str(e)

    # Kick off the scan in the background and return immediately. ``market`` is
    # an app-state singleton, so ``scanner`` stays valid after this request ends.
    task = asyncio.create_task(run_scan_task())
    _scan_tasks.add(task)
    task.add_done_callback(_scan_tasks.discard)

    return ScanExecuteResponse(
        scan_id=temp_scan_id,
        universe=request.universe,
        universe_size=len(tickers),
        message=f"Scan started across {len(tickers)} tickers.",
    )


@router.get("/{scan_id}/stream")
async def stream_scan_progress(scan_id: str):
    """
    SSE endpoint for real-time scan progress.
    """
    if scan_id not in _active_scans:
        raise HTTPException(status_code=404, detail="Scan not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        scan_state = _active_scans[scan_id]
        last_progress = None

        while True:
            current_progress = scan_state["progress"].copy()
            status = scan_state["status"]

            # Send progress update if changed
            if current_progress != last_progress:
                event = {
                    "type": "progress",
                    "scan_id": scan_id,
                    **current_progress,
                }
                yield f"data: {json.dumps(event)}\n\n"
                last_progress = current_progress

            # Send completion event
            if status == "complete":
                event = {
                    "type": "complete",
                    "scan_id": scan_id,
                    "total_analyzed": len(scan_state["results"]),
                }
                yield f"data: {json.dumps(event)}\n\n"
                break

            # Send error event
            if status == "error":
                event = {
                    "type": "error",
                    "scan_id": scan_id,
                    "error": scan_state.get("error", "Unknown error"),
                }
                yield f"data: {json.dumps(event)}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{scan_id}/results", response_model=ScanResultsResponse)
async def get_scan_results(scan_id: str):
    """
    Get results of a completed scan.
    """
    if scan_id not in _active_scans:
        raise HTTPException(status_code=404, detail="Scan not found")

    scan_state = _active_scans[scan_id]

    if scan_state["status"] == "running":
        # Serve a partial, score-sorted snapshot so research can start
        # while the scan runs (issue #53). Copy before sorting — Stage 2
        # appends to this list concurrently.
        partial = sorted(
            list(scan_state["results"]), key=lambda r: r["score"], reverse=True
        )
        return ScanResultsResponse(
            scan_id=scan_id,
            universe=scan_state["universe"],
            total_analyzed=len(partial),
            results=[ScanResultItem(**r) for r in partial],
            scanned_at=scan_state["started_at"],
            errors=scan_state.get("errors", 0),
            partial=True,
        )

    if scan_state["status"] == "error":
        raise HTTPException(status_code=500, detail=scan_state.get("error", "Scan failed"))

    errors = scan_state.get("errors", 0)
    total = scan_state["progress"].get("total", 0)
    warning = None

    if errors > 0 and total > 0:
        error_rate = errors / total
        if error_rate > 0.5:
            warning = f"High error rate: {errors}/{total} stocks failed analysis (likely API rate limiting). Try scanning a smaller universe or wait a few minutes."
        elif error_rate > 0.1:
            warning = f"{errors} stocks could not be analyzed (possible rate limiting)"

    return ScanResultsResponse(
        scan_id=scan_id,
        universe=scan_state["universe"],
        total_analyzed=len(scan_state["results"]),
        results=[ScanResultItem(**r) for r in scan_state["results"]],
        scanned_at=scan_state.get("completed_at", scan_state["started_at"]),
        errors=errors,
        warning=warning,
    )


@router.get("/ticker/{symbol}", response_model=ScanResultItem)
async def analyze_single_ticker(
    symbol: str,
    market: MarketData = Depends(get_market),
):
    """
    Analyze a single ticker with full technical breakdown.
    """
    try:
        analysis = await market.analysis(normalize_ticker(symbol))
    except TickerNotFound:
        raise HTTPException(status_code=404, detail=f"Could not analyze {symbol}")

    # ``analysis`` is a frozen Pydantic model with ``extra="allow"``; dump
    # it through ``ScanResultItem`` so the route response schema is
    # unchanged.
    return ScanResultItem(**analysis.model_dump())
