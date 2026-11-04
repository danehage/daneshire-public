"""Earnings-event drift + duplicate-merge engine (issue #75).

Finnhub moves report dates. :func:`upsert_finnhub_events` recognises an
incoming event as an existing row at a different date (same fiscal_period,
or within ±14 days when either side's fiscal_period is null) and moves that
row in place, so ``earnings_trades`` foreign keys survive.
:func:`merge_all_duplicates` cleans up duplicate pairs created before the
drift handling existed.

``POST /api/internal/earnings/refresh-calendar``,
``/earnings/backfill-history`` and ``/earnings/merge-duplicate-events`` are
thin wrappers over these functions. Nothing here commits; the routes own
the transaction.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.earnings import EarningsEvent
from app.models.earnings_trades import EarningsTrade
from app.schemas.earnings import DuplicateMergeSummary

logger = logging.getLogger(__name__)

DRIFT_WINDOW_DAYS = 14  # ±14 days for null-fiscal_period matching (#75)

# A past row with a realized move is never drifted (see find_drifted_row), so
# measuring one too early locks in a date that may only have been an
# estimate (#106). refresh-calendar's incoming dates are >= today, so a past
# row can still be drifted only while report_date >= today - DRIFT_WINDOW_DAYS.
# backfill-realized-moves waits until that window has closed.
REALIZED_MOVE_SETTLE_DAYS = DRIFT_WINDOW_DAYS


def realized_move_cutoff(today: date) -> date:
    """Exclusive upper bound on report_date for a realized-move measurement.
    A row dated before it can no longer be drifted by refresh-calendar."""
    return today - timedelta(days=REALIZED_MOVE_SETTLE_DAYS)


async def find_drifted_row(
    db: AsyncSession,
    ticker: str,
    incoming_date: date,
    fiscal_period: Optional[str],
) -> Optional[EarningsEvent]:
    """Return an existing row that represents the same event at a different date.

    Identity rules (issue #75):
    - Both non-null, same fiscal_period → same event.
    - Either side's fiscal_period is null → report_date within ±14 days of
      the incoming date (but not equal, since equal = same row via the
      ON CONFLICT path).

    Eligibility:
    - Future rows (report_date >= today) are always eligible.
    - Past rows are eligible only while unrealized — ``realized_move_pct``
      IS NULL — and only within ±14 days of the incoming date, even when
      fiscal_period matches. This is the "date slipped into the past"
      case: a Q3 row dated 10-01 that Finnhub moves to 10-08 on 10-02.
      Without it the stale 10-01 row was ignored, a second Q3 row was
      inserted, and the phantom past row (never reported on that date,
      so never given a realized move) lingered as a duplicate. A past row
      that already carries a realized move is a reported quarter and is
      never touched: those stay immutable.

    When several rows qualify, prefer future rows, then an exact
    fiscal_period match, then the nearest date.
    """
    today = date.today()
    low = incoming_date - timedelta(days=DRIFT_WINDOW_DAYS)
    high = incoming_date + timedelta(days=DRIFT_WINDOW_DAYS)
    in_window = and_(
        EarningsEvent.report_date >= low,
        EarningsEvent.report_date <= high,
    )

    eligible = or_(
        EarningsEvent.report_date >= today,
        and_(in_window, EarningsEvent.realized_move_pct.is_(None)),
    )

    if fiscal_period is not None:
        # Exact fiscal_period match, OR ±14-day window when the existing
        # row's fiscal_period is null ("either side's fiscal_period is null").
        same_event = or_(
            EarningsEvent.fiscal_period == fiscal_period,
            and_(EarningsEvent.fiscal_period.is_(None), in_window),
        )
    else:
        # Incoming fiscal_period is null → ±14-day window regardless of existing.
        same_event = in_window

    stmt = (
        select(EarningsEvent)
        .where(EarningsEvent.ticker == ticker)
        .where(EarningsEvent.report_date != incoming_date)
        .where(eligible)
        .where(same_event)
    )
    candidates = list((await db.execute(stmt)).scalars().all())
    if not candidates:
        return None

    def _rank(row: EarningsEvent) -> tuple:
        return (
            row.report_date < today,  # future rows first
            not (fiscal_period is not None and row.fiscal_period == fiscal_period),
            abs((row.report_date - incoming_date).days),
        )

    return min(candidates, key=_rank)


async def merge_events(
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


async def upsert_finnhub_events(
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
    row that represents the same event at a different date (matched by
    fiscal_period or ±14-day window; future rows, or past rows within the
    window that have no realized move yet — see ``find_drifted_row``).
    If found:
    - Updates its report_date in place (preserving its id and trade FKs).
    - If there is already a row at the incoming date, merges the two
      (repoints trades, deletes the loser).
    - Reported past events (realized_move_pct set) are never modified.

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
        drifted = await find_drifted_row(db, symbol, incoming_date, fiscal_period)
        if drifted is not None:
            # Existing row is the same event at a different date (future, or
            # a past date it never actually reported on): move it.
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
                await merge_events(db, keeper=drifted, loser=collision)

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


async def merge_all_duplicates(
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
        low = row.report_date - timedelta(days=DRIFT_WINDOW_DAYS)
        high = row.report_date + timedelta(days=DRIFT_WINDOW_DAYS)

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

        repointed = await merge_events(db, keeper=keeper, loser=loser)
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
