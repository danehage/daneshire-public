"""Decision log (#119): record buy/pass calls and summarise how they did.

Routes stay thin; the quote capture, the scan-pick lookup, the delete
window and the summary maths live here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.decision import Decision, DecisionOutcome
from app.models.scan_history import ScanPick
from app.schemas.decision import (
    HORIZONS,
    DecisionCreate,
    DecisionOutcomeResponse,
    DecisionResponse,
    DecisionSummaryCell,
    DecisionSummaryResponse,
)
from app.services.market import MarketData, MarketDataError

DELETE_WINDOW = timedelta(minutes=15)
MIN_RELIABLE_N = 10
SPY = "SPY"
_PRICE_Q = Decimal("0.0001")


class QuoteUnavailable(Exception):
    """The ticker or SPY quote couldn't be fetched; nothing was written."""


class SupersedeTargetInvalid(Exception):
    """``supersedes_id`` names no decision, or one on another ticker."""


class DecisionNotFound(Exception):
    pass


class DeleteWindowClosed(Exception):
    pass


async def create_decision(
    db: AsyncSession, market: MarketData, payload: DecisionCreate
) -> Decision:
    """Capture both quotes in one request, then write the frozen row."""
    if payload.supersedes_id is not None:
        target = await db.get(Decision, payload.supersedes_id)
        if target is None or target.ticker != payload.ticker:
            raise SupersedeTargetInvalid(
                "supersedes_id must name an earlier decision on the same ticker"
            )

    quotes = await market.quotes([payload.ticker, SPY])
    ticker_quote = quotes.get(payload.ticker)
    spy_quote = quotes.get(SPY)
    for symbol, quote in ((payload.ticker, ticker_quote), (SPY, spy_quote)):
        if quote is None or isinstance(quote, MarketDataError):
            raise QuoteUnavailable(f"No quote for {symbol}; decision not recorded")

    scan_pick_id = await _resolve_scan_pick(db, payload.scan_id, payload.ticker)

    decision = Decision(
        ticker=payload.ticker,
        decision=payload.decision,
        conviction=payload.conviction,
        thesis=payload.thesis,
        invalidation=payload.invalidation,
        price_at_decision=Decimal(str(ticker_quote.price)).quantize(_PRICE_Q),
        spy_at_decision=Decimal(str(spy_quote.price)).quantize(_PRICE_Q),
        source=payload.source,
        scan_pick_id=scan_pick_id,
        supersedes_id=payload.supersedes_id,
        tags=payload.tags,
    )
    db.add(decision)
    await db.flush()
    decision_id = decision.id
    await db.commit()
    return await _load(db, decision_id)


async def _resolve_scan_pick(
    db: AsyncSession, scan_id: Optional[str], ticker: str
) -> Optional[UUID]:
    if not scan_id:
        return None
    try:
        scan_uuid = UUID(scan_id)
    except ValueError:
        return None
    result = await db.execute(
        select(ScanPick.id)
        .where(ScanPick.scan_id == scan_uuid, ScanPick.ticker == ticker)
        .order_by(ScanPick.ranker.desc(), ScanPick.rank)  # 'scanner' before 'funnel'
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load(db: AsyncSession, decision_id: UUID) -> Decision:
    result = await db.execute(
        select(Decision)
        .options(selectinload(Decision.outcomes))
        .where(Decision.id == decision_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def list_decisions(
    db: AsyncSession,
    ticker: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = 200,
) -> list[DecisionResponse]:
    query = (
        select(Decision)
        .options(selectinload(Decision.outcomes))
        .order_by(Decision.created_at.desc())
        .limit(limit)
    )
    if ticker:
        query = query.where(Decision.ticker == ticker.upper())
    if decision:
        query = query.where(Decision.decision == decision)
    rows = (await db.execute(query)).scalars().all()

    superseded_by: dict[UUID, UUID] = {}
    if rows:
        later = await db.execute(
            select(Decision.supersedes_id, Decision.id)
            .where(Decision.supersedes_id.in_([r.id for r in rows]))
            .order_by(Decision.created_at)
        )
        for old_id, new_id in later.all():
            superseded_by.setdefault(old_id, new_id)

    return [to_response(r, superseded_by.get(r.id)) for r in rows]


def to_response(decision: Decision, superseded_by_id: Optional[UUID] = None) -> DecisionResponse:
    return DecisionResponse(
        id=decision.id,
        ticker=decision.ticker,
        decision=decision.decision,
        conviction=decision.conviction,
        thesis=decision.thesis,
        invalidation=decision.invalidation,
        price_at_decision=decision.price_at_decision,
        spy_at_decision=decision.spy_at_decision,
        source=decision.source,
        scan_pick_id=decision.scan_pick_id,
        supersedes_id=decision.supersedes_id,
        superseded_by_id=superseded_by_id,
        tags=list(decision.tags or []),
        created_at=decision.created_at,
        deletable_until=decision.created_at + DELETE_WINDOW,
        outcomes=[DecisionOutcomeResponse.model_validate(o) for o in decision.outcomes],
    )


async def delete_decision(
    db: AsyncSession, decision_id: UUID, now: Optional[datetime] = None
) -> None:
    """Delete within ``DELETE_WINDOW`` of ``created_at``; after that, never."""
    decision = await db.get(Decision, decision_id)
    if decision is None:
        raise DecisionNotFound()
    now = now or datetime.now(timezone.utc)
    if now - decision.created_at > DELETE_WINDOW:
        raise DeleteWindowClosed()
    await db.delete(decision)
    await db.commit()


@dataclass(frozen=True)
class _Scored:
    decision: str
    conviction: int
    horizon: int
    excess: float


def summarize(rows: Iterable[_Scored], total_decisions: int) -> DecisionSummaryResponse:
    """Group excess returns by call x conviction x horizon. Every cell of the
    grid is returned, empty ones with n=0."""
    groups: dict[tuple[str, int, int], list[float]] = {}
    for r in rows:
        groups.setdefault((r.decision, r.conviction, r.horizon), []).append(r.excess)

    cells = []
    for call in ("buy", "pass"):
        for conviction in range(5, 0, -1):
            for horizon in HORIZONS:
                values = groups.get((call, conviction, horizon), [])
                n = len(values)
                cells.append(
                    DecisionSummaryCell(
                        decision=call,
                        conviction=conviction,
                        horizon_sessions=horizon,
                        n=n,
                        mean_excess_return=statistics.fmean(values) if n else None,
                        median_excess_return=statistics.median(values) if n else None,
                        hit_rate=sum(1 for v in values if v > 0) / n if n else None,
                    )
                )
    return DecisionSummaryResponse(
        horizons=list(HORIZONS),
        min_reliable_n=MIN_RELIABLE_N,
        total_decisions=total_decisions,
        cells=cells,
    )


async def decision_summary(db: AsyncSession) -> DecisionSummaryResponse:
    """Superseded calls count: each row was a real call at a real time.
    ``no_data`` outcomes are excluded; ``truncated`` ones count."""
    result = await db.execute(
        select(
            Decision.decision,
            Decision.conviction,
            DecisionOutcome.horizon_sessions,
            DecisionOutcome.excess_return,
        )
        .join(DecisionOutcome, DecisionOutcome.decision_id == Decision.id)
        .where(
            DecisionOutcome.status.in_(("scored", "truncated")),
            DecisionOutcome.excess_return.is_not(None),
        )
    )
    rows = [
        _Scored(call, conviction, horizon, float(excess))
        for call, conviction, horizon, excess in result.all()
    ]
    total = (await db.execute(select(func.count()).select_from(Decision))).scalar_one()
    return summarize(rows, total)
