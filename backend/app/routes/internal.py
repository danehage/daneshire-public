"""
Internal API routes for Cloud Scheduler.

Each cron endpoint is a one-liner over :meth:`AlertEngine.run`. All
fetch / evaluate / history / notify logic lives in
``app.services.alert_engine``; routes only carry HTTP concerns.

Secured with the ``X-Scheduler-Secret`` header.
"""

import logging
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal

from sqlalchemy import func, select, update

from app.config import settings
from app.database import get_db
from app.models.earnings import EarningsEvent
from app.models.earnings_trades import EarningsTrade
from app.models.iv_snapshots import IVSnapshot
from app.models.portfolio import Account, HoldingGradeHistory
from app.models.watchlist import WatchlistItem
from app.routes.dependencies import get_alert_engine, get_portfolio_engine
from app.schemas.alert_runs import RunSummary
from app.schemas.backup import BackupResponse
from app.schemas.earnings import (
    BackfillRealizedMovesSummary,
    CalendarRefreshSummary,
    DuplicateMergeSummary,
    HistoryBackfillSummary,
    IVSnapshotRefreshSummary,
)
from app.schemas.iv import IVSnapshotRaw
from app.services.alert_engine import AlertEngine
from app.services.backup import run_backup
from app.services.indicators import TechnicalIndicators
from app.services.market import MarketData, EarningsDateUnknown, MarketDataError, get_market, compute_realized_move
from app.services.notifications import PushoverClient
from app.services.portfolio_engine import PortfolioEngine


# Per ADR-0004, the cutover from provider IV-rank to self-computed
# happens when this many prior rows exist for a ticker.
_IV_RANK_CUTOVER_ROWS = 252

# Stage-1 liquidity gate (#65) evaluates tickers with earnings events
# inside this many calendar days (~10 trading days, the screener window).
_GATE_HORIZON_DAYS = 14

# refresh-snapshots fetches + writes in batches this size so no DB
# transaction spans more than ~1 minute of Tastytrade calls. A gated set
# of ~150 tickers fetched in one go held a Neon connection idle inside an
# open transaction for ~7 minutes and it was closed server-side
# (InterfaceError in prod, 2026-07-16).
_SNAPSHOT_BATCH_SIZE = 25

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])


async def verify_scheduler_secret(
    x_scheduler_secret: Optional[str] = Header(default=None),
):
    """Verify the ``X-Scheduler-Secret`` header matches the config."""
    if not settings.scheduler_secret:
        # Development convenience: skip the check when nothing is set.
        if settings.environment == "development":
            logger.warning("Scheduler secret bypass - development mode")
            return
        raise HTTPException(status_code=500, detail="Scheduler secret not configured")

    if not x_scheduler_secret or not secrets.compare_digest(
        x_scheduler_secret, settings.scheduler_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")


@router.post("/alerts/run-price-checks", response_model=RunSummary)
async def run_price_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Evaluate all active ``price_cross`` alerts."""
    return await engine.run("price_cross")


@router.post("/alerts/run-technical-checks", response_model=RunSummary)
async def run_technical_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Evaluate all active ``technical_signal`` alerts."""
    return await engine.run("technical_signal")


@router.post("/alerts/run-reminders", response_model=RunSummary)
async def run_reminders(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Fire all ``date_reminder`` alerts whose trigger date is today."""
    return await engine.run("date_reminder")


@router.post("/alerts/run-earnings-checks", response_model=RunSummary)
async def run_earnings_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Evaluate all active ``earnings_iv`` alerts."""
    return await engine.run("earnings_iv")


@router.post("/alerts/expire-stale")
async def expire_stale_alerts(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Mark alerts past their ``expires_at`` as ``expired``."""
    expired = await engine.expire_stale_alerts()
    return {"status": "ok", "expired": expired}


_DRIFT_WINDOW_DAYS = 14  # ±14 days for null-fiscal_period matching (#75)


async def _find_drifted_row(
    db: AsyncSession,
    ticker: str,
    incoming_date: date,
    fiscal_period: Optional[str],
) -> Optional[EarningsEvent]:
    """Return an existing future row that represents the same event at a different date.

    Matching rules (issue #75):
    - Both non-null, same fiscal_period → exact match.
    - Either side's fiscal_period is null → report_date within ±14 days of
      the incoming date (but not equal, since equal = same row via the
      ON CONFLICT path).
    Only future rows (report_date >= today) are considered; past events are
    immutable.
    """
    from sqlalchemy import or_, and_

    today = date.today()
    low = incoming_date - timedelta(days=_DRIFT_WINDOW_DAYS)
    high = incoming_date + timedelta(days=_DRIFT_WINDOW_DAYS)

    if fiscal_period is not None:
        # Match by exact fiscal_period OR by ±14-day window when the existing
        # row has null fiscal_period (brief: "either side's fiscal_period is null").
        stmt = (
            select(EarningsEvent)
            .where(EarningsEvent.ticker == ticker)
            .where(EarningsEvent.report_date != incoming_date)
            .where(EarningsEvent.report_date >= today)
            .where(
                or_(
                    EarningsEvent.fiscal_period == fiscal_period,
                    and_(
                        EarningsEvent.fiscal_period.is_(None),
                        EarningsEvent.report_date >= low,
                        EarningsEvent.report_date <= high,
                    ),
                )
            )
            .limit(1)
        )
    else:
        # Incoming fiscal_period is null → ±14-day window regardless of existing.
        stmt = (
            select(EarningsEvent)
            .where(EarningsEvent.ticker == ticker)
            .where(EarningsEvent.report_date >= low)
            .where(EarningsEvent.report_date <= high)
            .where(EarningsEvent.report_date != incoming_date)
            .where(EarningsEvent.report_date >= today)
            .limit(1)
        )

    return (await db.execute(stmt)).scalars().first()


async def _merge_events(
    db: AsyncSession,
    keeper: EarningsEvent,
    loser: EarningsEvent,
) -> int:
    """Repoint all earnings_trades from loser → keeper, then delete loser.

    Returns the number of trade rows repointed.
    """
    result = await db.execute(
        update(EarningsTrade)
        .where(EarningsTrade.earnings_event_id == loser.id)
        .values(earnings_event_id=keeper.id)
    )
    repointed = result.rowcount
    await db.delete(loser)
    # The unit of work emits UPDATEs before DELETEs, so a caller that next
    # moves keeper onto loser's report_date would hit the unique constraint.
    # Flush now so the loser's row is gone before any such update.
    await db.flush()
    return repointed


async def _upsert_finnhub_events(
    db: AsyncSession,
    raw_events: list[dict],
    only_tickers: Optional[set[str]] = None,
) -> int:
    """Parse + upsert raw Finnhub calendar events; return the upsert count.

    Skips malformed rows. When ``only_tickers`` is given, events for other
    tickers are dropped before touching the database (the historical
    backfill pulls the full market and keeps only gated names). Existing
    rows keep their ``realized_move_pct`` — the conflict update never
    touches it.

    Drift handling (#75): before the plain upsert, looks for an existing
    future row that represents the same event at a different date
    (matched by fiscal_period or ±14-day window). If found:
    - Updates its report_date in place (preserving its id and trade FKs).
    - If there is already a row at the incoming date, merges the two
      (repoints trades, deletes the loser).
    - Past events are never modified.

    Does not commit.
    """
    today = date.today()
    upserted = 0

    for event in raw_events:
        symbol = (event.get("symbol") or "").strip().upper()
        date_str = event.get("date") or ""
        if not symbol or not date_str:
            continue
        if only_tickers is not None and symbol not in only_tickers:
            continue
        try:
            incoming_date = date.fromisoformat(date_str)
        except ValueError:
            continue

        raw_hour = (event.get("hour") or "unknown").lower()
        report_time = raw_hour if raw_hour in ("bmo", "amc") else "unknown"

        year = event.get("year")
        quarter = event.get("quarter")
        fiscal_period: Optional[str] = None
        if year and quarter:
            fiscal_period = f"Q{quarter} {year}"

        # --- Drift detection -------------------------------------------
        drifted = await _find_drifted_row(db, symbol, incoming_date, fiscal_period)
        if drifted is not None:
            # Existing row is a future event at a different date: move it.
            old_date = drifted.report_date
            logger.info(
                "refresh-calendar: %s %s date drifted %s → %s",
                symbol,
                fiscal_period or "(no fiscal_period)",
                old_date,
                incoming_date,
            )

            # Check whether there is already a row sitting at the target date
            # (a duplicate the bug already created on a prior refresh).
            collision_stmt = (
                select(EarningsEvent)
                .where(EarningsEvent.ticker == symbol)
                .where(EarningsEvent.report_date == incoming_date)
                .limit(1)
            )
            collision = (await db.execute(collision_stmt)).scalars().first()

            if collision is not None:
                # Merge: prefer the row with trades; prefer drifted (has the
                # old entry that trade FKs point at). Repoint all trades from
                # collision → drifted, then delete collision.
                logger.info(
                    "refresh-calendar: merging duplicate %s rows (%s / %s)",
                    symbol,
                    drifted.id,
                    collision.id,
                )
                await _merge_events(db, keeper=drifted, loser=collision)

            # Move the drifted row to the new date.
            drifted.report_date = incoming_date
            drifted.report_time = report_time
            drifted.fiscal_period = fiscal_period
            drifted.source = "finnhub"
            # If realized_move_pct was set and the date actually changed,
            # null it so the backfill recomputes it.
            if drifted.realized_move_pct is not None and old_date != incoming_date:
                drifted.realized_move_pct = None
            drifted.updated_at = datetime.now(timezone.utc)
            upserted += 1
            continue

        # --- Normal upsert (no drift) ----------------------------------
        stmt = (
            pg_insert(EarningsEvent)
            .values(
                ticker=symbol,
                report_date=incoming_date,
                report_time=report_time,
                fiscal_period=fiscal_period,
                source="finnhub",
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                constraint="uq_earnings_events_ticker_date",
                set_={
                    "report_time": report_time,
                    "fiscal_period": fiscal_period,
                    "source": "finnhub",
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await db.execute(stmt)
        upserted += 1

    return upserted


async def _merge_all_duplicates(
    db: AsyncSession,
) -> DuplicateMergeSummary:
    """Scan for and merge existing duplicate (ticker, same-quarter) pairs.

    A duplicate pair is two rows for the same ticker whose fiscal_periods
    match (or, when either is null, whose report_dates are within ±14 days).
    Past pairs are merged too — the duplicates this endpoint exists to clean
    (IR 07-29/07-30, ROL 07-21/07-29) are already in the past. Merging never
    rewrites the surviving row's report_date, so the past-events-immutable
    rule is preserved: we only delete the redundant record, keeping the row
    most likely to be the real event (trades > realized data > newer row,
    since the later insert reflects Finnhub's more recent view).

    Idempotent: re-running over a clean table is a no-op.
    Does not commit.
    """
    today = date.today()
    merges = 0
    trades_repointed = 0
    past_pairs_merged = 0

    # Iterate every event and look for a sibling in the same table,
    # deduplicating within a session-local seen set so a pair is only
    # processed once.
    all_stmt = select(EarningsEvent).order_by(
        EarningsEvent.ticker, EarningsEvent.report_date
    )
    all_rows = (await db.execute(all_stmt)).scalars().all()

    processed_ids: set = set()

    for row in all_rows:
        if row.id in processed_ids:
            continue

        # Find a sibling: same ticker, different id, same fiscal_period
        # OR within ±14 days when either side's fiscal_period is null.
        from sqlalchemy import or_, and_

        low = row.report_date - timedelta(days=_DRIFT_WINDOW_DAYS)
        high = row.report_date + timedelta(days=_DRIFT_WINDOW_DAYS)

        if row.fiscal_period is not None:
            sibling_stmt = (
                select(EarningsEvent)
                .where(EarningsEvent.ticker == row.ticker)
                .where(EarningsEvent.id != row.id)
                .where(
                    or_(
                        EarningsEvent.fiscal_period == row.fiscal_period,
                        and_(
                            EarningsEvent.fiscal_period.is_(None),
                            EarningsEvent.report_date >= low,
                            EarningsEvent.report_date <= high,
                        ),
                    )
                )
                .limit(1)
            )
        else:
            sibling_stmt = (
                select(EarningsEvent)
                .where(EarningsEvent.ticker == row.ticker)
                .where(EarningsEvent.id != row.id)
                .where(EarningsEvent.report_date >= low)
                .where(EarningsEvent.report_date <= high)
                .limit(1)
            )

        sibling = (await db.execute(sibling_stmt)).scalars().first()
        if sibling is None:
            continue

        # Decide keeper: trades > realized data > newer row (the later
        # insert reflects Finnhub's more recent view of the date).
        keeper, loser = row, sibling
        has_trades_row = await _has_trades(db, row.id)
        has_trades_sib = await _has_trades(db, sibling.id)
        if has_trades_sib != has_trades_row:
            if has_trades_sib:
                keeper, loser = sibling, row
        elif (sibling.realized_move_pct is not None) != (
            row.realized_move_pct is not None
        ):
            if sibling.realized_move_pct is not None:
                keeper, loser = sibling, row
        elif (
            sibling.created_at is not None
            and row.created_at is not None
            and sibling.created_at > row.created_at
        ):
            keeper, loser = sibling, row

        processed_ids.add(keeper.id)
        processed_ids.add(loser.id)

        logger.info(
            "merge-duplicates: %s keeping %s (%s), dropping %s (%s)",
            row.ticker,
            keeper.id,
            keeper.report_date,
            loser.id,
            loser.report_date,
        )

        repointed = await _merge_events(db, keeper=keeper, loser=loser)
        merges += 1
        trades_repointed += repointed
        if keeper.report_date < today and loser.report_date < today:
            past_pairs_merged += 1

    return DuplicateMergeSummary(
        merges=merges,
        trades_repointed=trades_repointed,
        past_pairs_merged=past_pairs_merged,
    )


async def _has_trades(db: AsyncSession, event_id) -> bool:
    """Return True if any earnings_trades row points at this event."""
    stmt = select(func.count()).select_from(EarningsTrade).where(
        EarningsTrade.earnings_event_id == event_id
    )
    return int((await db.execute(stmt)).scalar() or 0) > 0


@router.post("/earnings/refresh-calendar", response_model=CalendarRefreshSummary)
async def refresh_earnings_calendar(
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """Pull the next 4 weeks of earnings events from Finnhub and upsert them."""
    today = date.today()
    end = today + timedelta(days=28)

    try:
        raw_events = await market.earnings_calendar(today, end)
    except EarningsDateUnknown as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    upserted = await _upsert_finnhub_events(db, raw_events)
    await db.commit()
    logger.info("refresh-calendar: upserted %d events (%s → %s)", upserted, today, end)
    return CalendarRefreshSummary(upserted=upserted, start=today, end=end)


@router.post("/earnings/merge-duplicate-events", response_model=DuplicateMergeSummary)
async def merge_duplicate_earnings_events(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """One-time idempotent cleanup: merge duplicate (ticker, same-quarter) event rows.

    When Finnhub previously moved a report date, the old refresh logic
    inserted a new row instead of updating the existing one (#75). This
    endpoint finds those stale duplicate pairs, repoints any
    ``earnings_trades`` rows from the loser to the keeper, and deletes the
    loser. Running it twice on a clean table is a no-op.

    Runs within a single transaction committed here; the summary reflects
    what was actually done this call.
    """
    summary = await _merge_all_duplicates(db)
    await db.commit()
    logger.info(
        "merge-duplicate-events: %d pairs merged, %d trades repointed, %d fully-past pairs among them",
        summary.merges,
        summary.trades_repointed,
        summary.past_pairs_merged,
    )
    return summary


async def _gate_upcoming_tickers(
    db: AsyncSession,
    market: MarketData,
) -> tuple[set[str], set[str], dict[str, int]]:
    """Stage-1 liquidity gate (#65, PRD §15).

    Returns ``(tagged, passed_gate, gate_counts)``: `earnings-candidate`
    watchlist tickers (manual override, never evaluated), the subset of
    tickers with events inside `_GATE_HORIZON_DAYS` whose FMP quote clears
    price / avg-volume / market-cap thresholds (today's volume stands in
    when the provider omits the average; quote errors count as failed),
    and the evaluated / passed_gate / failed_gate counts.
    """
    today = date.today()

    stmt = select(WatchlistItem.ticker).where(WatchlistItem.tags.any("earnings-candidate"))
    rows = (await db.execute(stmt)).scalars().all()
    tagged = {(t or "").strip().upper() for t in rows if t}

    # Stage-1 candidates: distinct tickers with an event inside the horizon.
    horizon = today + timedelta(days=_GATE_HORIZON_DAYS)
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


@router.post("/earnings/refresh-snapshots", response_model=IVSnapshotRefreshSummary)
async def refresh_iv_snapshots(
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
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

    tagged, passed_gate, gate_counts = await _gate_upcoming_tickers(db, market)

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

    for batch_start in range(0, len(tickers), _SNAPSHOT_BATCH_SIZE):
        batch = tickers[batch_start : batch_start + _SNAPSHOT_BATCH_SIZE]
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

            iv_rank, source = await _resolve_iv_rank(db, ticker, result)

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
            min(batch_start + _SNAPSHOT_BATCH_SIZE, len(tickers)),
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


def _clamp_rank(rank: Decimal) -> Decimal:
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


async def _resolve_iv_rank(
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

    if row_count < _IV_RANK_CUTOVER_ROWS:
        return _clamp_rank(raw.iv_rank_provider), "tastytrade"

    recent = (
        select(IVSnapshot.iv30)
        .where(IVSnapshot.ticker == ticker)
        .order_by(IVSnapshot.snapshot_date.desc())
        .limit(_IV_RANK_CUTOVER_ROWS)
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
        return _clamp_rank(raw.iv_rank_provider), "tastytrade"

    rank = (raw.iv30 - min_iv) / (max_iv - min_iv) * Decimal(100)
    return _clamp_rank(rank), "self_252d"


@router.post("/earnings/backfill-realized-moves", response_model=BackfillRealizedMovesSummary)
async def backfill_realized_moves(
    limit: int = Query(default=50, ge=1, le=500),
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """Compute and persist ``realized_move_pct`` for past earnings events.

    Idempotent — events already having a value are skipped. Processes up
    to ``limit`` events per call (default 50) for pagination-friendly
    operation under Cloud Run's request timeout. Oldest events first.
    """
    today = date.today()

    # Fetch past events missing realized_move_pct, oldest first.
    stmt = (
        select(EarningsEvent)
        .where(EarningsEvent.report_date < today)
        .where(EarningsEvent.realized_move_pct.is_(None))
        .order_by(EarningsEvent.report_date.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    total_events = len(rows)

    # Group by ticker to avoid repeated FMP fetches for the same symbol.
    from collections import defaultdict
    by_ticker: dict[str, list[EarningsEvent]] = defaultdict(list)
    for row in rows:
        by_ticker[row.ticker].append(row)

    processed = 0
    skipped_no_history: list[str] = []

    for ticker, events in by_ticker.items():
        try:
            frame = await market.history(ticker, days=1500)
        except MarketDataError:
            skipped_no_history.append(ticker)
            continue

        bars = [{"date": str(b.date), "close": float(b.close)} for b in frame.bars]
        for event in events:
            move = compute_realized_move(bars, event.report_date, event.report_time)
            if move is not None:
                event.realized_move_pct = move
                event.updated_at = datetime.now(timezone.utc)
                processed += 1

    await db.commit()
    logger.info(
        "backfill-realized-moves: processed %d events, skipped %d tickers",
        processed,
        len(skipped_no_history),
    )
    return BackfillRealizedMovesSummary(
        processed=processed,
        skipped_no_history=skipped_no_history,
        total_events=total_events,
    )


_HISTORY_CHUNK_DAYS = 30


@router.post("/earnings/backfill-history", response_model=HistoryBackfillSummary)
async def backfill_earnings_history(
    quarters: int = Query(default=8, ge=1, le=12),
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """One-shot historical calendar backfill (#66, PRD §15).

    Pulls the past ``quarters`` quarters of Finnhub earnings events in
    30-day chunks and upserts only the tickers the Stage-1 gate currently
    admits (plus `earnings-candidate` tags). Gives newly gated tickers the
    past-event rows the edge ratio needs; realized moves themselves are
    computed by the existing paginated ``backfill-realized-moves``.

    Idempotent — re-running upserts in place and never touches
    ``realized_move_pct``. Failed chunks are skipped and counted so a
    partial Finnhub outage doesn't lose the whole run; re-run to fill gaps.
    Commits per chunk, so a request timeout keeps completed chunks.
    """
    today = date.today()
    end = today - timedelta(days=1)
    start = today - timedelta(days=quarters * 92)

    tagged, passed_gate, _gate_counts = await _gate_upcoming_tickers(db, market)
    targets = tagged | passed_gate

    summary = HistoryBackfillSummary(
        start=start,
        end=end,
        target_tickers=len(targets),
        events_upserted=0,
        chunks_total=0,
        chunks_empty=0,
        chunks_failed=0,
    )
    if not targets:
        return summary

    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=_HISTORY_CHUNK_DAYS - 1), end)
        summary.chunks_total += 1
        try:
            raw_events = await market.earnings_calendar(cursor, chunk_end)
        except EarningsDateUnknown as exc:
            logger.warning(
                "backfill-history: chunk %s → %s failed: %s", cursor, chunk_end, exc
            )
            summary.chunks_failed += 1
            cursor = chunk_end + timedelta(days=1)
            continue

        upserted = await _upsert_finnhub_events(db, raw_events, only_tickers=targets)
        await db.commit()
        if upserted == 0:
            summary.chunks_empty += 1
        summary.events_upserted += upserted
        cursor = chunk_end + timedelta(days=1)

    logger.info(
        "backfill-history: %d events for %d tickers (%s → %s), "
        "%d/%d chunks empty, %d failed",
        summary.events_upserted,
        summary.target_tickers,
        start,
        end,
        summary.chunks_empty,
        summary.chunks_total,
        summary.chunks_failed,
    )
    return summary


@router.post("/db/backup", response_model=BackupResponse)
async def run_db_backup(
    _: None = Depends(verify_scheduler_secret),
):
    """Dump the production Neon database and upload it to GCS.

    Cloud Scheduler calls this nightly. A non-2xx response causes
    Cloud Scheduler to mark the execution as failed and retry.
    """
    if not settings.gcs_backup_bucket:
        raise HTTPException(status_code=400, detail="GCS_BACKUP_BUCKET not configured")

    result = await run_backup(settings.neon_database_url, settings.gcs_backup_bucket)

    if not result.success:
        pushover = PushoverClient()
        if pushover.is_configured:
            await pushover.send(
                title="DB Backup Failed",
                message=f"pg_dump → {result.gcs_path} failed: {result.error}",
                priority="high",
            )
            await pushover.close()
        raise HTTPException(
            status_code=500,
            detail=f"Backup failed: {result.error}",
        )

    return BackupResponse(
        success=result.success,
        filename=result.filename,
        gcs_path=result.gcs_path,
        size_bytes=result.size_bytes,
        duration_seconds=result.duration_seconds,
        error=result.error,
    )


# Grade severity for worsening-detection: higher = more urgent to act.
_GRADE_SEVERITY: dict[str, int] = {
    "Running": 0,
    "Cresting": 1,
    "Distribution": 2,
    "Thesis complete": 3,
    "Climax": 4,
    "Broken": 5,
}


@router.post("/portfolio/run-plateau-checks", response_model=RunSummary)
async def run_plateau_checks(
    engine: PortfolioEngine = Depends(get_portfolio_engine),
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """Run sell-side plateau grades for all equity holdings across all accounts.

    Grades are logged to ``holding_grade_history``. Pushover fires when a grade
    worsens (severity increases) since the last run, plus one daily refresher
    while a holding stays deteriorated. Recovery to Running is silent.
    """
    now = datetime.now(timezone.utc)

    # Collect all equity tickers + entry cost across accounts.
    acc_result = await db.execute(select(Account.id))
    account_ids = list(acc_result.scalars().all())

    ticker_open_dates: dict[str, Optional[str]] = {}
    ticker_entry_price: dict[str, Optional[float]] = {}
    for aid in account_ids:
        holdings = await engine.current_holdings(aid)
        for h in holdings:
            if h.instrument_type != "equity":
                continue
            t = h.ticker.upper()
            if t not in ticker_open_dates:
                ticker_open_dates[t] = None
                ticker_entry_price[t] = (
                    float(h.avg_cost) if h.avg_cost is not None else None
                )

    if not ticker_open_dates:
        return RunSummary(alert_type="plateau_grade")

    # Resolve open_date from watchlist items (entry_date field).
    tickers = list(ticker_open_dates.keys())
    wl_result = await db.execute(
        select(WatchlistItem.ticker, WatchlistItem.entry_date)
        .where(WatchlistItem.ticker.in_(tickers))
        .where(WatchlistItem.status == "owned")
    )
    for wl_ticker, entry_date in wl_result.all():
        t = wl_ticker.upper()
        if t in ticker_open_dates and ticker_open_dates[t] is None and entry_date is not None:
            ticker_open_dates[t] = str(entry_date)

    unresolved = sorted(t for t, d in ticker_open_dates.items() if d is None)
    if unresolved:
        # Mis-anchoring is the failure mode the #57 design flagged as mandatory
        # to avoid — make it loud when the open date can't be resolved.
        logger.warning(
            "plateau-check: no open date for %s — run anchoring falls back to "
            "trailing 126 bars and the gap gauge is disabled for these",
            ", ".join(unresolved),
        )

    # Previous grades for worsening-detection; last ping for the daily refresher.
    prev_grades = await engine.latest_grades(tickers)
    last_notified = await engine.last_notified_at(tickers)

    met = not_met = fired = errored = 0
    pushover = PushoverClient()

    for ticker, open_date in ticker_open_dates.items():
        try:
            # 700 calendar days ≈ 480 bars: covers the 252-bar grading window
            # plus the pre-entry 52-week window the gap gauge needs.
            frame = await market.history(ticker, days=700)
        except MarketDataError:
            logger.warning("plateau-check: no history for %s — skipped", ticker)
            errored += 1
            continue

        if not frame.bars:
            not_met += 1
            continue

        df_full = pd.DataFrame([
            {
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in frame.bars
        ])
        df_full["date"] = pd.to_datetime(df_full["date"])
        df_full = df_full.sort_values("date").reset_index(drop=True)

        # Pre-entry 52w high (the level the knockdown fell from) — highest high
        # of the 252 bars ending at the open date. Drives the gap-closed gauge
        # and the Thesis complete label.
        pre_entry_high = None
        if open_date is not None:
            pre = df_full[df_full["date"] <= pd.Timestamp(open_date)].tail(252)
            if len(pre) >= 60:
                pre_entry_high = float(pre["high"].max())

        df = df_full.tail(252).reset_index(drop=True)
        current_price = float(df["close"].iloc[-1])

        metrics = TechnicalIndicators.calculate_plateau_metrics(
            current_price=current_price,
            df=df,
            open_date=open_date,
            pre_entry_high_52w=pre_entry_high,
            entry_price=ticker_entry_price.get(ticker),
        )
        if metrics is None:
            not_met += 1
            continue

        metrics["price"] = current_price
        label, score, signals, stop_suggestion = TechnicalIndicators.grade_holding(metrics)

        # ATR as % of price — display-only alongside HV, never scoring.
        atr14 = metrics.get("atr14")
        atr_pct_val = (
            round(atr14 / current_price * 100, 2)
            if atr14 is not None and current_price > 0
            else None
        )
        hv_now_val = metrics.get("hv_now")

        row = HoldingGradeHistory(
            ticker=ticker,
            checked_at=now,
            sell_grade=label,
            sell_pressure_score=score,
            signals=signals,
            stop_suggestion=stop_suggestion,
            hv_now=hv_now_val,
            atr_pct=atr_pct_val,
            notified=False,
        )
        db.add(row)
        met += 1

        # Fire Pushover on worsening (any severity increase), plus one daily
        # refresher while deteriorated. Recovery to Running stays silent.
        prev = prev_grades.get(ticker)
        prev_severity = _GRADE_SEVERITY.get(prev.sell_grade, 0) if prev else -1
        new_severity = _GRADE_SEVERITY.get(label, 0)
        worsened = new_severity > prev_severity and new_severity > 0
        last_ping = last_notified.get(ticker)
        refresher_due = (
            new_severity > 0
            and not worsened
            and (last_ping is None or last_ping.date() < now.date())
        )
        if (worsened or refresher_due) and pushover.is_configured:
            stop_str = f" Stop: {stop_suggestion}" if stop_suggestion else ""
            await pushover.send(
                title=f"{ticker} — {label}",
                message=f"Sell pressure {score:+d}.{stop_str} Signals: {'; '.join(signals[:2]) or 'none'}",
                priority="high" if label in ("Climax", "Broken") else "normal",
            )
            row.notified = True
            fired += 1

    await db.commit()
    if pushover.is_configured:
        await pushover.close()

    logger.info(
        "plateau-checks: met=%d fired=%d not_met=%d errored=%d",
        met, fired, not_met, errored,
    )
    return RunSummary(
        alert_type="plateau_grade",
        met=met,
        not_met=not_met,
        errored=errored,
        notifications_sent=fired,
    )


@router.get("/health")
async def internal_health(
    _: None = Depends(verify_scheduler_secret),
):
    """Health check endpoint for Cloud Scheduler monitoring.

    Requires the scheduler secret like every other internal route. It used
    to be unauthenticated and reported whether Pushover and the scheduler
    secret were configured, which told an anonymous caller what to target.
    Cloud Run's own liveness probe uses the public /api/health instead.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
