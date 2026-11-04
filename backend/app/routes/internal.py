"""
Internal API routes for Cloud Scheduler.

Each cron endpoint is a one-liner over :meth:`AlertEngine.run`. All
fetch / evaluate / history / notify logic lives in
``app.services.alert_engine``; routes only carry HTTP concerns.

Secured with the ``X-Scheduler-Secret`` header.
"""

import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.earnings import EarningsEvent
from app.routes.dependencies import get_alert_engine, get_portfolio_engine
from app.schemas.alert_runs import RunSummary
from app.schemas.backup import BackupResponse
from app.schemas.decision import DecisionScoreSummary
from app.schemas.earnings import (
    BackfillRealizedMovesSummary,
    CalendarRefreshSummary,
    DuplicateMergeSummary,
    HistoryBackfillSummary,
    IVSnapshotRefreshSummary,
)
from app.services.alert_engine import AlertEngine
from app.services.backup import run_backup
from app.services.decision_scoring import SpyHistoryUnavailable, score_due_decisions
from app.services.indicators import current_et_time, is_regular_session
from app.services.market import MarketData, EarningsDateUnknown, MarketDataError, get_market, compute_realized_move
from app.services import earnings_merge, iv_snapshots, plateau
from app.services.notifications import PushoverClient
from app.services.portfolio_engine import PortfolioEngine


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


def get_now_et() -> datetime:
    """Clock dependency for the market-hours gate (overridable in tests)."""
    return current_et_time()


async def _run_during_session(
    engine: AlertEngine, alert_type: str, now_et: datetime
) -> RunSummary:
    """Run ``alert_type`` only inside the regular session.

    The price/technical cron is ``*/15 9-16 * * 1-5``, which fires
    09:00–16:45 ET; the spec is 09:30–16:00. Gating here (not in the cron
    expression, not in ``AlertEngine.run``) keeps the manual
    ``POST /api/alerts/evaluate`` ungated. Holidays are not detected
    (ADR-0001), so holiday weekdays still evaluate.
    """
    if not is_regular_session(now_et):
        logger.info(
            "%s checks skipped: outside market hours (%s ET)",
            alert_type,
            now_et.strftime("%a %H:%M"),
        )
        return RunSummary(alert_type=alert_type)
    return await engine.run(alert_type)


def _fail_if_upstream_outage(summary: RunSummary) -> RunSummary:
    """Turn a provider-caused all-errored run into a 502 for Cloud Scheduler.

    When FMP rejects the key, every price/technical evaluation errors, but
    these endpoints used to answer 200 and the Scheduler console showed
    green. Only provider failures count: a run whose sole alert is on a
    delisted ticker also errors every time, but that is the alert's fault
    and answers 200 (the engine logs it at WARNING) rather than failing
    the job every 15 minutes.

    The history rows are already committed by the engine (every evaluation
    is logged to alert_history); this only changes what the scheduler is
    told. The manual ``POST /api/alerts/evaluate`` keeps returning the
    summary as-is — it is a debugging surface.
    """
    if summary.upstream_outage:
        raise HTTPException(
            status_code=502,
            detail=(
                f"alert-engine {summary.alert_type}: all {summary.errored} "
                "evaluations errored; market data provider unavailable"
            ),
        )
    return summary


@router.post("/alerts/run-price-checks", response_model=RunSummary)
async def run_price_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
    now_et: datetime = Depends(get_now_et),
):
    """Evaluate all active ``price_cross`` alerts (regular session only)."""
    return _fail_if_upstream_outage(
        await _run_during_session(engine, "price_cross", now_et)
    )


@router.post("/alerts/run-technical-checks", response_model=RunSummary)
async def run_technical_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
    now_et: datetime = Depends(get_now_et),
):
    """Evaluate all active ``technical_signal`` alerts (regular session only)."""
    return _fail_if_upstream_outage(
        await _run_during_session(engine, "technical_signal", now_et)
    )


@router.post("/alerts/run-reminders", response_model=RunSummary)
async def run_reminders(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Fire all ``date_reminder`` alerts whose trigger date is today."""
    return _fail_if_upstream_outage(await engine.run("date_reminder"))


@router.post("/alerts/run-earnings-checks", response_model=RunSummary)
async def run_earnings_checks(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Evaluate all active ``earnings_iv`` alerts."""
    return _fail_if_upstream_outage(await engine.run("earnings_iv"))


@router.post("/alerts/expire-stale")
async def expire_stale_alerts(
    engine: AlertEngine = Depends(get_alert_engine),
    _: None = Depends(verify_scheduler_secret),
):
    """Mark alerts past their ``expires_at`` as ``expired``."""
    expired = await engine.expire_stale_alerts()
    return {"status": "ok", "expired": expired}


# Finnhub's /calendar/earnings silently truncates at 1500 rows. A single
# 28-day request plateaued at exactly 1500 upserts once October earnings
# season filled the window (883 → 1147 → 1420 → 1500 → 1500, 2026-09-25..
# 10-02), dropping everything past the cap. Fetch in weekly chunks instead.
_CALENDAR_CHUNK_DAYS = 7
_FINNHUB_CALENDAR_ROW_CAP = 1500


async def _fetch_calendar_range(market: MarketData, start: date, end: date) -> list[dict]:
    """Fetch ``start..end`` (inclusive); halve and refetch while capped.

    A response with ``>= _FINNHUB_CALENDAR_ROW_CAP`` rows is presumed
    truncated: log a WARNING and split the range in half, down to single
    days. A single day still at the cap is kept as-is (nothing smaller to
    ask for) with its own WARNING. ``EarningsDateUnknown`` propagates.
    """
    events = await market.earnings_calendar(start, end)
    if len(events) < _FINNHUB_CALENDAR_ROW_CAP:
        return events
    if start >= end:
        logger.warning(
            "refresh-calendar: single day %s returned %d rows (Finnhub cap %d) "
            "— events may be missing",
            start,
            len(events),
            _FINNHUB_CALENDAR_ROW_CAP,
        )
        return events
    mid = start + timedelta(days=(end - start).days // 2)
    logger.warning(
        "refresh-calendar: chunk %s → %s returned %d rows (Finnhub cap %d) "
        "— splitting at %s and refetching",
        start,
        end,
        len(events),
        _FINNHUB_CALENDAR_ROW_CAP,
        mid,
    )
    first = await _fetch_calendar_range(market, start, mid)
    second = await _fetch_calendar_range(market, mid + timedelta(days=1), end)
    return first + second


async def _fetch_calendar_chunked(
    market: MarketData,
    start: date,
    end: date,
    chunk_days: int = _CALENDAR_CHUNK_DAYS,
) -> list[dict]:
    """Fetch the Finnhub calendar for ``start..end`` in ``chunk_days`` chunks.

    Chunks are contiguous and non-overlapping. Results are concatenated and
    deduped by (symbol, date), first occurrence wins. Any chunk failure
    (``EarningsDateUnknown``) propagates, so the caller fails the whole run
    exactly as it did for the former single request — no partial upsert.
    """
    events: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        for event in await _fetch_calendar_range(market, cursor, chunk_end):
            key = ((event.get("symbol") or "").strip().upper(), event.get("date") or "")
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
        cursor = chunk_end + timedelta(days=1)
    return events


@router.post("/earnings/refresh-calendar", response_model=CalendarRefreshSummary)
async def refresh_earnings_calendar(
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_scheduler_secret),
):
    """Pull the next 4 weeks of earnings events from Finnhub and upsert them.

    Fetched in weekly chunks to stay under Finnhub's 1500-row response cap
    (see ``_fetch_calendar_chunked``). A failed chunk fails the run (502).
    """
    today = date.today()
    end = today + timedelta(days=28)

    try:
        raw_events = await _fetch_calendar_chunked(market, today, end)
    except EarningsDateUnknown as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    upserted = await earnings_merge.upsert_finnhub_events(db, raw_events)
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
    summary = await earnings_merge.merge_all_duplicates(db)
    await db.commit()
    logger.info(
        "merge-duplicate-events: %d pairs merged, %d trades repointed, %d fully-past pairs among them",
        summary.merges,
        summary.trades_repointed,
        summary.past_pairs_merged,
    )
    return summary


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
    prior rows < 252; self-computed thereafter. The work lives in
    :mod:`app.services.iv_snapshots`.
    """
    return await iv_snapshots.refresh_iv_snapshots(db, market)


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

    Events less than ``REALIZED_MOVE_SETTLE_DAYS`` old are left for a later
    run: their date may still be an estimate Finnhub corrects, and a
    measured row can no longer be moved (#106).
    """
    cutoff = earnings_merge.realized_move_cutoff(date.today())

    # Fetch settled past events missing realized_move_pct, oldest first.
    stmt = (
        select(EarningsEvent)
        .where(EarningsEvent.report_date < cutoff)
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

    tagged, passed_gate, _gate_counts = await iv_snapshots.gate_upcoming_tickers(db, market)
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

        upserted = await earnings_merge.upsert_finnhub_events(db, raw_events, only_tickers=targets)
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

    Cloud Scheduler calls this nightly. A non-2xx response marks the
    execution as failed. The job has no retries, because a same-day re-run
    500s by design: the runtime SA cannot overwrite the day's object.
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
    while a holding stays deteriorated. Recovery to Running is silent. The
    engine lives in :mod:`app.services.plateau`.
    """
    return await plateau.run_plateau_checks(db, engine, market)


@router.post("/decisions/score", response_model=DecisionScoreSummary)
async def score_decisions(
    _: None = Depends(verify_scheduler_secret),
    market: MarketData = Depends(get_market),
    db: AsyncSession = Depends(get_db),
    now_et: datetime = Depends(get_now_et),
):
    """Score decision-log outcomes that have come due (#119).

    Meant for a weekday 18:30 ET Cloud Scheduler job, after EOD bars post.
    Idempotent: it only inserts (decision, horizon) rows that don't exist
    yet, so a retry or a manual re-run is safe and a missed day catches up
    on the next run. 502 when SPY history can't be fetched, since no
    session can be counted without it; nothing is written in that case.
    """
    try:
        return await score_due_decisions(db, market, now_et)
    except SpyHistoryUnavailable as exc:
        raise HTTPException(
            status_code=502, detail=f"decision scoring: SPY history unavailable ({exc})"
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
