"""Daily IV-snapshot refresh for the earnings screener (#65, ADR-0003, ADR-0004).

Writes one ``iv_snapshots`` row per ticker that has an upcoming earnings
event and clears the Stage-1 liquidity gate, or carries the
``earnings-candidate`` watchlist tag. The IV-rank source per row follows
ADR-0004 (:func:`resolve_iv_rank`).

``POST /api/internal/earnings/refresh-snapshots`` is a thin wrapper around
:func:`refresh_iv_snapshots`. :func:`gate_upcoming_tickers` is also used by
``POST /api/internal/earnings/backfill-history`` to pick its target tickers.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.earnings import EarningsEvent
from app.models.iv_snapshots import IVSnapshot
from app.models.watchlist import WatchlistItem
from app.schemas.earnings import IVSnapshotRefreshSummary
from app.schemas.iv import IVSnapshotRaw
from app.services.market import MarketData, MarketDataError

logger = logging.getLogger(__name__)

# Per ADR-0004, the cutover from provider IV-rank to self-computed
# happens when this many prior rows exist for a ticker.
IV_RANK_CUTOVER_ROWS = 252

# Stage-1 liquidity gate (#65) evaluates tickers with earnings events
# inside this many calendar days (~10 trading days, the screener window).
GATE_HORIZON_DAYS = 14

# refresh-snapshots fetches + writes in batches this size so no DB
# transaction spans more than ~1 minute of Tastytrade calls. A gated set
# of ~150 tickers fetched in one go held a Neon connection idle inside an
# open transaction for ~7 minutes and it was closed server-side
# (InterfaceError in prod, 2026-07-16).
SNAPSHOT_BATCH_SIZE = 25


async def gate_upcoming_tickers(
    db: AsyncSession,
    market: MarketData,
) -> tuple[set[str], set[str], dict[str, int]]:
    """Stage-1 liquidity gate (#65, PRD §15).

    Returns ``(tagged, passed_gate, gate_counts)``: `earnings-candidate`
    watchlist tickers (manual override, never evaluated), the subset of
    tickers with events inside `GATE_HORIZON_DAYS` whose FMP quote clears
    price / avg-volume / market-cap thresholds (today's volume stands in
    when the provider omits the average; quote errors count as failed),
    and the evaluated / passed_gate / failed_gate counts.
    """
    today = date.today()

    stmt = select(WatchlistItem.ticker).where(WatchlistItem.tags.any("earnings-candidate"))
    rows = (await db.execute(stmt)).scalars().all()
    tagged = {(t or "").strip().upper() for t in rows if t}

    # Stage-1 candidates: distinct tickers with an event inside the horizon.
    horizon = today + timedelta(days=GATE_HORIZON_DAYS)
    stmt = (
        select(EarningsEvent.ticker)
        .where(EarningsEvent.report_date >= today)
        .where(EarningsEvent.report_date <= horizon)
        .distinct()
    )
    rows = (await db.execute(stmt)).scalars().all()
    candidates = sorted(
        {(t or "").strip().upper() for t in rows if t} - tagged
    )

    # Release the connection before ANY provider I/O. The FMP quote sweep
    # alone can run minutes at season peak (the bulk endpoint currently
    # fails per-chunk and falls back to ~600 throttled individual
    # requests), and Neon closes connections left idle inside an open
    # transaction (prod InterfaceError twice on 2026-07-16 — first during
    # the Tastytrade fetch, then during this quote sweep).
    await db.commit()

    passed_gate: set[str] = set()
    if candidates:
        quote_map = await market.quotes(candidates)
        for ticker in candidates:
            q = quote_map.get(ticker)
            if isinstance(q, MarketDataError) or q is None:
                continue
            if (
                q.price >= settings.earnings_gate_min_price
                and (q.avg_volume or q.volume) >= settings.earnings_gate_min_avg_volume
                and q.market_cap >= settings.earnings_gate_min_market_cap
            ):
                passed_gate.add(ticker)

    evaluated = len(candidates)
    gate_counts = {
        "evaluated": evaluated,
        "passed_gate": len(passed_gate),
        "failed_gate": evaluated - len(passed_gate),
    }
    return tagged, passed_gate, gate_counts


async def refresh_iv_snapshots(
    db: AsyncSession, market: MarketData
) -> IVSnapshotRefreshSummary:
    """Write today's `iv_snapshots` row for every ticker that either has an
    upcoming earnings event AND clears the Stage-1 liquidity gate, or carries
    the `earnings-candidate` watchlist tag (manual override, gate bypassed).

    The gate (#65, PRD §15): FMP batch quotes on tickers with events in the
    next 14 calendar days; keep price ≥ EARNINGS_GATE_MIN_PRICE, average
    volume ≥ EARNINGS_GATE_MIN_AVG_VOLUME (today's volume when the provider
    omits the average), market cap ≥ EARNINGS_GATE_MIN_MARKET_CAP. Quote
    errors count as failed.

    Skips tickers with no data (no chain / no quote / no token) and
    tickers whose front-week straddle could not be priced (ADR-0003).
    Per-ticker IV-rank source follows ADR-0004: provider value while
    prior rows < 252; self-computed thereafter.
    """
    today = date.today()

    tagged, passed_gate, gate_counts = await gate_upcoming_tickers(db, market)

    tickers = sorted(passed_gate | tagged)
    if not tickers:
        return IVSnapshotRefreshSummary(
            snapshot_date=today,
            written=0,
            skipped_no_data=[],
            skipped_rejected_move=[],
            **gate_counts,
        )

    written = 0
    skipped_no_data: list[str] = []
    skipped_rejected_move: list[str] = []
    skip_reasons: Counter[str] = Counter()

    for batch_start in range(0, len(tickers), SNAPSHOT_BATCH_SIZE):
        batch = tickers[batch_start : batch_start + SNAPSHOT_BATCH_SIZE]
        snapshots = await market.iv_snapshots(batch)

        for ticker in batch:
            result = snapshots.get(ticker)
            if isinstance(result, MarketDataError):
                skipped_no_data.append(ticker)
                skip_reasons[getattr(result, "reason", "error")] += 1
                continue
            if not isinstance(result, IVSnapshotRaw):  # defensive
                skipped_no_data.append(ticker)
                skip_reasons["error"] += 1
                continue
            if result.expected_move_pct is None:
                skipped_rejected_move.append(ticker)
                skip_reasons["rejected_move"] += 1
                continue

            iv_rank, source = await resolve_iv_rank(db, ticker, result)

            stmt = (
                pg_insert(IVSnapshot)
                .values(
                    ticker=ticker,
                    snapshot_date=today,
                    iv30=result.iv30,
                    iv_rank=iv_rank,
                    expected_move_pct=result.expected_move_pct,
                    source=source,
                )
                .on_conflict_do_nothing(constraint="uq_iv_snapshots_ticker_date")
            )
            await db.execute(stmt)
            written += 1

        # Commit per batch: completed batches survive a request timeout,
        # and on_conflict_do_nothing makes a rerun cheap.
        await db.commit()
        logger.info(
            "refresh-snapshots: %d/%d tickers done",
            min(batch_start + SNAPSHOT_BATCH_SIZE, len(tickers)),
            len(tickers),
        )

    logger.info(
        "refresh-snapshots: gate %d/%d passed (+%d tagged), wrote %d, no-data %d, rejected %d, reasons %s",
        gate_counts["passed_gate"],
        gate_counts["evaluated"],
        len(tagged),
        written,
        len(skipped_no_data),
        len(skipped_rejected_move),
        dict(skip_reasons),
    )
    return IVSnapshotRefreshSummary(
        snapshot_date=today,
        written=written,
        skipped_no_data=skipped_no_data,
        skipped_rejected_move=skipped_rejected_move,
        skip_reasons=dict(skip_reasons),
        **gate_counts,
    )


def clamp_rank(rank: Decimal) -> Decimal:
    """Bound an IV rank to [0, 100] (`iv_rank_bounded` check constraint).

    Tastytrade's rank is (current − 52w low) / (52w high − 52w low) and
    legitimately escapes [0, 1] when current IV breaches its own yearly
    range — common for volatile names at earnings-season peak (first seen
    in prod on the gated universe, 2026-07-16, as a CheckViolationError).
    """
    if rank < 0:
        return Decimal(0)
    if rank > 100:
        return Decimal(100)
    return rank


async def resolve_iv_rank(
    db: AsyncSession, ticker: str, raw: IVSnapshotRaw
) -> tuple[Decimal, str]:
    """Pick the IV-rank source per ADR-0004.

    < 252 prior rows → provider value, ``source='tastytrade'``.
    ≥ 252 prior rows → self-computed from min/max(iv30) over the most
    recent 252 rows, ``source='self_252d'``. Both clamped to [0, 100].
    """
    count_stmt = select(func.count()).select_from(IVSnapshot).where(
        IVSnapshot.ticker == ticker
    )
    row_count = int((await db.execute(count_stmt)).scalar() or 0)

    if row_count < IV_RANK_CUTOVER_ROWS:
        return clamp_rank(raw.iv_rank_provider), "tastytrade"

    recent = (
        select(IVSnapshot.iv30)
        .where(IVSnapshot.ticker == ticker)
        .order_by(IVSnapshot.snapshot_date.desc())
        .limit(IV_RANK_CUTOVER_ROWS)
        .subquery()
    )
    bounds_stmt = select(
        func.min(recent.c.iv30).label("min_iv"),
        func.max(recent.c.iv30).label("max_iv"),
    )
    bounds = (await db.execute(bounds_stmt)).one()
    min_iv = bounds.min_iv
    max_iv = bounds.max_iv
    if min_iv is None or max_iv is None or max_iv == min_iv:
        # Degenerate range — fall back to provider value rather than
        # writing a meaningless 0 or 100.
        return clamp_rank(raw.iv_rank_provider), "tastytrade"

    rank = (raw.iv30 - min_iv) / (max_iv - min_iv) * Decimal(100)
    return clamp_rank(rank), "self_252d"
