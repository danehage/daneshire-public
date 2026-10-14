"""
Forward record of scan picks (ADR-0007).

Every completed scan writes its quality-gated rows to ``scan_picks`` so the
scanner's real-time list can be scored against what happened next.

Since #117 the scanner is a sortable shortlist, not a ranker, so the record
holds the FULL gated list (decided 2026-10-13). ``rank`` is still the
legacy-score order, and ``payload.legacy_top20`` marks the rows the old
top-20 record would have kept, so the forward record stays comparable with
pre-#117 scans: filter on that flag to get the old record back. The
in-memory scan store in ``routes/scanner.py`` ages out within the hour;
this is what survives.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scan_history import ScanPick

logger = logging.getLogger(__name__)

TOP_N = 20

# Result fields worth keeping alongside the pick for later attribution.
_PAYLOAD_KEYS = (
    "signals", "trend", "rsi", "hv_rank", "dist_50", "dist_200",
    "range_position", "high_52w", "low_52w", "market_cap",
    "rsi_floor_delta", "days_since_low", "post_low_trend",
    "earnings_date", "days_to_earnings",
    "quality_pass", "quality_fails", "dollar_volume",
    # #117 fact columns
    "pct_off_high", "sector", "last_earnings_date", "last_eps_actual",
    "last_eps_estimate", "eps_surprise_pct", "earnings_reaction_pct",
    "earnings_reaction_vs_spy",
    # #126: the rest of what the results table's expanded row shows, so a
    # saved scan renders like a live one. Rows recorded before #126 lack
    # these; the page shows them blank.
    "support", "resistance", "ma_slope", "volume", "avg_volume",
    "volume_pace", "volume_pace_reliable", "current_hv", "momentum_10d",
    "pct_above_low",
)


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


async def record_scan_picks(
    session: AsyncSession,
    scan_id: str,
    universe: str,
    results: list[dict],
    ranker: str = "scanner",
    top_n: int = TOP_N,
    scanned_at: Optional[datetime] = None,
) -> int:
    """Persist every shown row of ``results`` (sorted by legacy score).

    Rows the quality gate failed are hidden from the scanner by default
    (#116), so they are not recorded: ranks count gate passers (and rows
    the gate couldn't judge) only. Rows recorded since #116 carry
    ``quality_pass`` in their payload; older rows predate the gate.

    Every recorded row's payload has ``legacy_top20`` (#117): true for the
    first ``top_n`` ranks. Rows from before #117 have no such key; they
    are all top-20 picks.

    Returns the number of rows written. Rows without a positive price are
    skipped (nothing to measure a forward return from) but keep their rank
    slot, so ranks reflect the scanner's real ordering.
    """
    scanned_at = scanned_at or datetime.now(timezone.utc)
    rows = []
    shown = [r for r in results if r.get("quality_pass") is not False]
    for rank, r in enumerate(shown, start=1):
        price = r.get("price")
        if not price or price <= 0:
            continue
        rows.append(
            ScanPick(
                scan_id=UUID(str(scan_id)),
                universe=universe,
                ranker=ranker,
                rank=rank,
                ticker=r["ticker"],
                score=r.get("score"),
                setup=r.get("setup"),
                tier=r.get("tier"),
                price=price,
                payload={
                    **{k: _jsonable(r.get(k)) for k in _PAYLOAD_KEYS if r.get(k) is not None},
                    "legacy_top20": rank <= top_n,
                },
                scanned_at=scanned_at,
            )
        )
    session.add_all(rows)
    await session.commit()
    return len(rows)


def _pick_to_result(pick: ScanPick) -> dict:
    """Rebuild a results-table row from a recorded pick."""
    payload = {k: v for k, v in (pick.payload or {}).items() if k != "legacy_top20"}
    return {
        **payload,
        "ticker": pick.ticker,
        "price": float(pick.price),
        "score": int(pick.score) if pick.score is not None else None,
        "setup": pick.setup,
    }


async def load_latest_scan(session: AsyncSession, ranker: str = "scanner") -> Optional[dict]:
    """The most recent recorded scan, shaped for the scanner page (#126).

    Returns ``None`` when nothing has been recorded. Rows come back in
    rank order. Only shown rows were recorded (gate failures never are),
    so a saved scan has no "filtered out" rows to offer.
    """
    latest = (
        await session.execute(
            select(ScanPick.scan_id, ScanPick.universe, ScanPick.scanned_at)
            .where(ScanPick.ranker == ranker)
            .order_by(ScanPick.scanned_at.desc(), ScanPick.created_at.desc())
            .limit(1)
        )
    ).first()
    if latest is None:
        return None

    picks = (
        await session.execute(
            select(ScanPick)
            .where(ScanPick.scan_id == latest.scan_id, ScanPick.ranker == ranker)
            .order_by(ScanPick.rank)
        )
    ).scalars().all()
    return {
        "scan_id": str(latest.scan_id),
        "universe": latest.universe,
        "scanned_at": latest.scanned_at,
        "results": [_pick_to_result(p) for p in picks],
    }
