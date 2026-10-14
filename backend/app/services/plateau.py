"""Sell-side plateau grading + notification engine (issue #57).

Grades every equity holding across all accounts, logs each grade to
``holding_grade_history`` and fires Pushover when a grade worsens, plus one
daily refresher while a holding stays deteriorated. Recovery to Running is
silent. ETFs are skipped (issue #137): run-topping sell alerts don't apply to
index or income funds.

``POST /api/internal/portfolio/run-plateau-checks`` is a thin wrapper around
:func:`run_plateau_checks`. :func:`grade_bars` is the windowing + grading
step on its own, so the fixture tests exercise the exact code production
runs instead of a copy of it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.portfolio import Account, HoldingGradeHistory
from app.models.watchlist import WatchlistItem
from app.schemas.alert_runs import RunSummary
from app.schemas.market import HistoryBar
from app.services.indicators import TechnicalIndicators
from app.services.market import MarketData, MarketDataError
from app.services.notifications import PushoverClient
from app.services.portfolio_engine import PortfolioEngine

logger = logging.getLogger(__name__)

# Grade severity for worsening-detection: higher = more urgent to act.
GRADE_SEVERITY: dict[str, int] = {
    "Running": 0,
    "Cresting": 1,
    "Distribution": 2,
    "Thesis complete": 3,
    "Climax": 4,
    "Broken": 5,
}


@dataclass(frozen=True)
class PlateauGrade:
    """One holding's grade. ``metrics`` includes ``price`` (= current_price)."""

    label: str
    score: int
    signals: list[str]
    stop_suggestion: Optional[str]
    current_price: float
    metrics: dict


async def _etf_flag(market: MarketData, ticker: str) -> bool:
    """True when FMP's profile flags ``ticker`` as an ETF.

    A failed or empty profile lookup returns False so the ticker is graded
    as before: a missed ETF costs one stray alert, a wrongly skipped stock
    costs a missed sell signal.
    """
    try:
        return await market.is_etf(ticker)
    except MarketDataError as exc:
        logger.warning(
            "plateau-check: ETF lookup failed for %s (%s) — grading it", ticker, exc
        )
        return False


def grade_bars(
    bars: Sequence[HistoryBar],
    open_date: Optional[str],
    entry_price: Optional[float],
) -> Optional[PlateauGrade]:
    """Window ``bars`` and run the plateau grade.

    ``bars`` must be non-empty. Returns ``None`` when there are too few bars
    for :meth:`TechnicalIndicators.calculate_plateau_metrics`.
    """
    df_full = pd.DataFrame([
        {
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
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
        entry_price=entry_price,
    )
    if metrics is None:
        return None

    metrics["price"] = current_price
    label, score, signals, stop_suggestion = TechnicalIndicators.grade_holding(metrics)
    return PlateauGrade(
        label=label,
        score=score,
        signals=signals,
        stop_suggestion=stop_suggestion,
        current_price=current_price,
        metrics=metrics,
    )


async def run_plateau_checks(
    db: AsyncSession,
    engine: PortfolioEngine,
    market: MarketData,
) -> RunSummary:
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

    # Skip ETFs: not graded, logged, notified or warned about.
    candidates = list(ticker_open_dates.keys())
    etf_flags = await asyncio.gather(*(_etf_flag(market, t) for t in candidates))
    etfs = sorted(t for t, is_etf in zip(candidates, etf_flags) if is_etf)
    for t in etfs:
        del ticker_open_dates[t]
        del ticker_entry_price[t]
    if etfs:
        logger.info("plateau-check: skipping ETFs %s", ", ".join(etfs))
    if not ticker_open_dates:
        logger.info(
            "plateau-checks: met=0 fired=0 not_met=0 errored=0 skipped_etf=%d",
            len(etfs),
        )
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

    try:
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

            grade = grade_bars(frame.bars, open_date, ticker_entry_price.get(ticker))
            if grade is None:
                not_met += 1
                continue

            label = grade.label
            score = grade.score
            signals = grade.signals
            stop_suggestion = grade.stop_suggestion
            current_price = grade.current_price

            # ATR as % of price — display-only alongside HV, never scoring.
            atr14 = grade.metrics.get("atr14")
            atr_pct_val = (
                round(atr14 / current_price * 100, 2)
                if atr14 is not None and current_price > 0
                else None
            )
            hv_now_val = grade.metrics.get("hv_now")

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
            prev_severity = GRADE_SEVERITY.get(prev.sell_grade, 0) if prev else -1
            new_severity = GRADE_SEVERITY.get(label, 0)
            worsened = new_severity > prev_severity and new_severity > 0
            last_ping = last_notified.get(ticker)
            refresher_due = (
                new_severity > 0
                and not worsened
                and (last_ping is None or last_ping.date() < now.date())
            )
            if (worsened or refresher_due) and pushover.is_configured:
                stop_str = f" Stop: {stop_suggestion}" if stop_suggestion else ""
                sent = await pushover.send(
                    title=f"{ticker} — {label}",
                    message=f"Sell pressure {score:+d}.{stop_str} Signals: {'; '.join(signals[:2]) or 'none'}",
                    priority="high" if label in ("Climax", "Broken") else "normal",
                )
                # Only a delivered push counts: ``notified`` drives the daily
                # refresher (last_notified_at), so marking a failed send as
                # notified would suppress the retry until tomorrow.
                if sent:
                    row.notified = True
                    fired += 1
                else:
                    logger.warning(
                        "plateau-check: push for %s (%s) not delivered", ticker, label
                    )

        await db.commit()
    finally:
        # Always release the HTTP client, even if grading or the commit raised.
        await pushover.close()

    logger.info(
        "plateau-checks: met=%d fired=%d not_met=%d errored=%d skipped_etf=%d",
        met, fired, not_met, errored, len(etfs),
    )
    return RunSummary(
        alert_type="plateau_grade",
        met=met,
        not_met=not_met,
        errored=errored,
        notifications_sent=fired,
    )
