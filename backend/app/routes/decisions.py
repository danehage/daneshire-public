"""Decision log routes (#119). Owner-only: ``/api/decisions`` is not in the
guest allowlist, and deliberately not under ``/api/ticker`` (guest-reachable).

There is no PATCH: decisions are insert-only. A change of mind is a new
decision with ``supersedes_id`` set.
"""

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.decision import DecisionCreate, DecisionResponse, DecisionSummaryResponse
from app.services.decisions import (
    DecisionNotFound,
    DeleteWindowClosed,
    QuoteUnavailable,
    SupersedeTargetInvalid,
    create_decision,
    decision_summary,
    delete_decision,
    list_decisions,
    to_response,
)
from app.services.market import MarketData, get_market

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.post("", response_model=DecisionResponse, status_code=201)
async def record_decision(
    payload: DecisionCreate,
    db: AsyncSession = Depends(get_db),
    market: MarketData = Depends(get_market),
):
    """Record a buy/pass call. The server takes the ticker and SPY quotes in
    one request; if either fails nothing is written (502)."""
    try:
        decision = await create_decision(db, market, payload)
    except QuoteUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except SupersedeTargetInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return to_response(decision)


@router.get("", response_model=list[DecisionResponse])
async def get_decisions(
    ticker: Optional[str] = Query(default=None, max_length=10),
    decision: Optional[Literal["buy", "pass"]] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Decisions, newest first, with their scored outcomes embedded."""
    return await list_decisions(db, ticker=ticker, decision=decision, limit=limit)


@router.get("/summary", response_model=DecisionSummaryResponse)
async def get_decision_summary(db: AsyncSession = Depends(get_db)):
    """Excess return vs SPY by call x conviction x horizon."""
    return await decision_summary(db)


@router.delete("/{decision_id}", status_code=204)
async def remove_decision(decision_id: UUID, db: AsyncSession = Depends(get_db)):
    """Allowed within 15 minutes of recording, for typos. After that, 409:
    record a superseding decision instead."""
    try:
        await delete_decision(db, decision_id)
    except DecisionNotFound:
        raise HTTPException(status_code=404, detail="Decision not found")
    except DeleteWindowClosed:
        raise HTTPException(
            status_code=409,
            detail="Decisions can only be deleted within 15 minutes; record a superseding decision instead",
        )
