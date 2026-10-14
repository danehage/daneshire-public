"""Scores decision-log entries against SPY at 21 / 63 / 126 sessions (#119).

Rules (approved in #119):

* **Anchor**: ``price_at_decision`` and ``spy_at_decision``, the two quotes
  taken in one request when the call was recorded. No look-ahead, and both
  legs cover the same window.
* **Sessions are counted on SPY's own bars.** Session 1 is the first SPY
  close after ``created_at``; horizon N exits at the Nth. Holidays and
  half-days come out right without a market calendar (ADR-0001).
* **Return**: price return on both legs, no costs. ``excess = ticker − SPY``;
  passes are not sign-flipped.
* **Truncated**: the ticker's data ends before the exit date and SPY has
  printed more than 5 sessions since its last bar (delisted, acquired,
  renamed). Scored at the ticker's last close, with SPY measured to the
  same date.
* **No data**: nothing usable for the ticker 10 sessions after the horizon
  came due. Kept visible, excluded from stats.

The pure functions here take bars and return drafts; ``score_due_decisions``
does the I/O. Inserts use ON CONFLICT DO NOTHING on (decision, horizon), so
a re-run or a scheduler retry never writes twice, and a missed day catches
up on the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Sequence
from uuid import UUID

import pytz
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import Decision, DecisionOutcome
from app.schemas.decision import HORIZONS, DecisionScoreSummary
from app.services.market import MarketData, MarketDataError, TickerNotFound

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")

# A bar's close is taken to happen at 16:00 ET on its date. Half-days close
# at 13:00, so a call made between 13:00 and 16:00 on a half-day counts that
# day's close as session 1 although it printed earlier. Rare and small.
SESSION_CLOSE_ET = time(16, 0)

# A bar dated today is only trusted once the EOD data has settled. Before
# this, FMP's daily series can carry today's still-moving price as a bar.
BAR_FINAL_AFTER_ET = time(17, 0)

TRUNCATE_AFTER_SESSIONS = 5
NO_DATA_AFTER_SESSIONS = 10

# The decision-time quote and the series' close before the decision should
# agree to within a day's move. When they disagree by more than this ratio
# the ticker split (FMP's daily series is split-adjusted, the stored quote
# is not) and the quote can't anchor the adjusted exit price.
SPLIT_RATIO_THRESHOLD = 1.5

SPY = "SPY"

_PRICE_Q = Decimal("0.0001")
_RETURN_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Bar:
    date: date
    close: float


@dataclass(frozen=True)
class DecisionInput:
    created_at: datetime
    price_at_decision: float
    spy_at_decision: float


@dataclass(frozen=True)
class OutcomeDraft:
    horizon_sessions: int
    status: str
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    spy_exit_price: Optional[float] = None
    ticker_return: Optional[float] = None
    spy_return: Optional[float] = None
    excess_return: Optional[float] = None


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------


def bars_from_history(raw_bars: Iterable, now_et: datetime) -> list[Bar]:
    """Oldest-first ``Bar`` list from history bars, dropping unsettled ones.

    Accepts anything with ``date`` (``YYYY-MM-DD``) and ``close`` attributes
    or keys. Bars dated after today, or dated today before
    ``BAR_FINAL_AFTER_ET``, are dropped: an exit must never be scored on a
    close that hasn't happened or is still moving.
    """
    now_et = _to_et(now_et)
    today = now_et.date()
    settled_today = now_et.time() >= BAR_FINAL_AFTER_ET
    out: dict[date, Bar] = {}
    for raw in raw_bars:
        get = raw.get if isinstance(raw, dict) else lambda k, r=raw: getattr(r, k, None)
        try:
            d = date.fromisoformat(str(get("date"))[:10])
            close = float(get("close"))
        except (TypeError, ValueError):
            continue
        if not close > 0:
            continue
        if d > today or (d == today and not settled_today):
            continue
        out[d] = Bar(d, close)
    return [out[d] for d in sorted(out)]


def session_close(d: date) -> datetime:
    return _ET.localize(datetime.combine(d, SESSION_CLOSE_ET))


def sessions_after(decided_at: datetime, spy_bars: Sequence[Bar]) -> list[Bar]:
    """SPY bars whose close came after the decision: session 1, 2, ..."""
    return [b for b in spy_bars if session_close(b.date) > decided_at]


def score_horizon(
    decision: DecisionInput,
    horizon: int,
    spy_bars: Sequence[Bar],
    ticker_bars: Optional[Sequence[Bar]],
) -> Optional[OutcomeDraft]:
    """Score one horizon, or return None when it can't be decided yet.

    ``spy_bars`` and ``ticker_bars`` are settled, oldest-first bars (see
    ``bars_from_history``). ``ticker_bars`` is None or empty when the
    provider had nothing for the ticker.
    """
    spy_after = sessions_after(decision.created_at, spy_bars)
    if len(spy_after) < horizon:
        return None  # not due

    spy_exit_bar = spy_after[horizon - 1]
    sessions_past_due = len(spy_after) - horizon
    first_session = spy_after[0].date

    ticker_bars = list(ticker_bars or [])
    ticker_after = [b for b in ticker_bars if b.date >= first_session]
    if not ticker_after:
        if sessions_past_due >= NO_DATA_AFTER_SESSIONS:
            return OutcomeDraft(horizon_sessions=horizon, status="no_data")
        return None

    status = "scored"
    on_or_before_exit = [b for b in ticker_after if b.date <= spy_exit_bar.date]
    data_continues = ticker_after[-1].date >= spy_exit_bar.date
    if data_continues:
        if not on_or_before_exit:
            # No print at all between the decision and the exit date.
            if sessions_past_due >= NO_DATA_AFTER_SESSIONS:
                return OutcomeDraft(horizon_sessions=horizon, status="no_data")
            return None
        # Normally the bar on the exit date; a halted day falls back to the
        # last close before it.
        ticker_exit = on_or_before_exit[-1]
        spy_exit = spy_exit_bar
    else:
        last = ticker_after[-1]
        spy_sessions_since_last = sum(1 for b in spy_after if b.date > last.date)
        if spy_sessions_since_last <= TRUNCATE_AFTER_SESSIONS:
            return None  # the ticker's data may just be lagging
        status = "truncated"
        ticker_exit = last
        spy_exit = [b for b in spy_after if b.date <= last.date][-1]

    ticker_anchor = decision.price_at_decision
    spy_anchor = decision.spy_at_decision

    # Split guard: compare the quote with the adjusted close just before the
    # decision. If they disagree by a split-sized ratio, anchor both legs on
    # that close instead, so both still cover the same window.
    before = [b for b in ticker_bars if b.date < first_session]
    if before and ticker_anchor > 0:
        ratio = ticker_anchor / before[-1].close
        if ratio > SPLIT_RATIO_THRESHOLD or ratio < 1 / SPLIT_RATIO_THRESHOLD:
            spy_before = [b for b in spy_bars if b.date <= before[-1].date]
            if spy_before:
                logger.warning(
                    "decision anchor %.4f vs prior adjusted close %.4f (ratio %.2f): "
                    "split suspected, anchoring on the %s close",
                    ticker_anchor, before[-1].close, ratio, before[-1].date,
                )
                ticker_anchor = before[-1].close
                spy_anchor = spy_before[-1].close

    ticker_return = ticker_exit.close / ticker_anchor - 1
    spy_return = spy_exit.close / spy_anchor - 1
    return OutcomeDraft(
        horizon_sessions=horizon,
        status=status,
        exit_date=ticker_exit.date,
        exit_price=ticker_exit.close,
        spy_exit_price=spy_exit.close,
        ticker_return=ticker_return,
        spy_return=spy_return,
        excess_return=ticker_return - spy_return,
    )


def plan_outcomes(
    decision: DecisionInput,
    existing_horizons: Iterable[int],
    spy_bars: Sequence[Bar],
    ticker_bars: Optional[Sequence[Bar]],
) -> list[OutcomeDraft]:
    """Drafts for every horizon that is due and not already scored."""
    done = set(existing_horizons)
    drafts = []
    for horizon in HORIZONS:
        if horizon in done:
            continue
        draft = score_horizon(decision, horizon, spy_bars, ticker_bars)
        if draft is not None:
            drafts.append(draft)
    return drafts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class SpyHistoryUnavailable(Exception):
    """SPY bars couldn't be fetched, so no session can be counted."""


async def score_due_decisions(
    db: AsyncSession, market: MarketData, now_et: datetime
) -> DecisionScoreSummary:
    """Insert outcome rows for every due, unscored horizon.

    Raises ``SpyHistoryUnavailable`` when SPY can't be fetched; nothing is
    written in that case.
    """
    now_et = _to_et(now_et)
    outcome_count = (
        select(DecisionOutcome.decision_id, func.count().label("n"))
        .group_by(DecisionOutcome.decision_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Decision)
            .outerjoin(outcome_count, outcome_count.c.decision_id == Decision.id)
            .where(func.coalesce(outcome_count.c.n, 0) < len(HORIZONS))
            .order_by(Decision.created_at)
        )
    ).scalars().all()

    summary = DecisionScoreSummary(decisions_checked=len(rows))
    if not rows:
        return summary

    existing = await _existing_horizons(db, [d.id for d in rows])

    # Enough bars to reach back past the oldest pending decision (bars are
    # never more numerous than calendar days).
    oldest = min(d.created_at for d in rows)
    days = max(200, (now_et - oldest.astimezone(_ET)).days + 30)

    try:
        spy_frame = await market.history_uncached(SPY, days=days)
    except MarketDataError as exc:
        raise SpyHistoryUnavailable(str(exc)) from exc
    spy_bars = bars_from_history(spy_frame.bars, now_et)
    if not spy_bars:
        raise SpyHistoryUnavailable("SPY history is empty")

    ticker_bars: dict[str, Optional[list[Bar]]] = {}
    unavailable: set[str] = set()
    for ticker in sorted({d.ticker for d in rows}):
        try:
            frame = await market.history_uncached(ticker, days=days)
            ticker_bars[ticker] = bars_from_history(frame.bars, now_et)
        except TickerNotFound:
            # The client reports a provider outage as "no data" too; only a
            # genuine no-data answer may run down the no_data clock.
            failure = _upstream_failure(market)
            if failure is not None:
                logger.warning("decision scoring: %s history unavailable (%s)", ticker, failure)
                unavailable.add(ticker)
            else:
                ticker_bars[ticker] = None
        except MarketDataError as exc:
            logger.warning("decision scoring: %s history unavailable (%s)", ticker, exc)
            unavailable.add(ticker)

    for decision in rows:
        done = existing.get(decision.id, set())
        missing = len(HORIZONS) - len(done)
        if decision.ticker in unavailable:
            summary.pending += missing
            continue
        drafts = plan_outcomes(
            DecisionInput(
                created_at=decision.created_at,
                price_at_decision=float(decision.price_at_decision),
                spy_at_decision=float(decision.spy_at_decision),
            ),
            done,
            spy_bars,
            ticker_bars.get(decision.ticker),
        )
        summary.pending += missing - len(drafts)
        for draft in drafts:
            inserted = await _insert_outcome(db, decision.id, draft)
            if inserted:
                setattr(summary, draft.status, getattr(summary, draft.status) + 1)

    summary.tickers_unavailable = sorted(unavailable)
    await db.commit()
    logger.info(
        "decision scoring: %d checked, %d scored, %d truncated, %d no_data, %d pending",
        summary.decisions_checked, summary.scored, summary.truncated,
        summary.no_data, summary.pending,
    )
    return summary


async def _existing_horizons(db: AsyncSession, ids: list[UUID]) -> dict[UUID, set[int]]:
    result = await db.execute(
        select(DecisionOutcome.decision_id, DecisionOutcome.horizon_sessions).where(
            DecisionOutcome.decision_id.in_(ids)
        )
    )
    out: dict[UUID, set[int]] = {}
    for decision_id, horizon in result.all():
        out.setdefault(decision_id, set()).add(horizon)
    return out


async def _insert_outcome(db: AsyncSession, decision_id: UUID, draft: OutcomeDraft) -> bool:
    stmt = (
        pg_insert(DecisionOutcome)
        .values(
            decision_id=decision_id,
            horizon_sessions=draft.horizon_sessions,
            status=draft.status,
            exit_date=draft.exit_date,
            exit_price=_q(draft.exit_price, _PRICE_Q),
            spy_exit_price=_q(draft.spy_exit_price, _PRICE_Q),
            ticker_return=_q(draft.ticker_return, _RETURN_Q),
            spy_return=_q(draft.spy_return, _RETURN_Q),
            excess_return=_q(draft.excess_return, _RETURN_Q),
        )
        .on_conflict_do_nothing(index_elements=["decision_id", "horizon_sessions"])
    )
    result = await db.execute(stmt)
    return (result.rowcount or 0) > 0


def _q(value: Optional[float], quantum: Decimal) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def _upstream_failure(market) -> Optional[str]:
    probe = getattr(market, "upstream_failure", None)
    return probe() if callable(probe) else None


def _to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return _ET.localize(dt)
    return dt.astimezone(_ET)
