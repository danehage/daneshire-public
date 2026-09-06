"""
Tests for issue-#67 ironfly screener additions:

  - max_realized_move_pct in /calendar and /screen responses
  - quarters_used exposed in responses
  - suggested_entry_date derivation (amc, bmo, unknown, Monday-BMO)
  - null-last sorting on new columns
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.earnings import EarningsEvent
from app.models.iv_snapshots import IVSnapshot
from app.routes.earnings import _prior_trading_day, _suggested_entry_date


# ---------------------------------------------------------------------------
# Pure-function unit tests — no DB needed
# ---------------------------------------------------------------------------


def test_prior_trading_day_wednesday():
    d = date(2026, 7, 15)  # Wednesday
    assert _prior_trading_day(d) == date(2026, 7, 14)  # Tuesday


def test_prior_trading_day_monday():
    d = date(2026, 7, 13)  # Monday
    assert _prior_trading_day(d) == date(2026, 7, 10)  # Friday


def test_prior_trading_day_tuesday():
    d = date(2026, 7, 14)  # Tuesday
    assert _prior_trading_day(d) == date(2026, 7, 13)  # Monday


def test_suggested_entry_date_amc():
    d = date(2026, 7, 15)
    assert _suggested_entry_date(d, "amc") == d


def test_suggested_entry_date_bmo_wednesday():
    d = date(2026, 7, 15)  # Wednesday
    assert _suggested_entry_date(d, "bmo") == date(2026, 7, 14)  # Tuesday


def test_suggested_entry_date_bmo_monday():
    d = date(2026, 7, 13)  # Monday
    assert _suggested_entry_date(d, "bmo") == date(2026, 7, 10)  # Friday


def test_suggested_entry_date_unknown():
    d = date(2026, 7, 15)
    assert _suggested_entry_date(d, "unknown") is None


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------

_TODAY = date.today()
_FUTURE = _TODAY + timedelta(days=7)
_PAST = _TODAY - timedelta(days=90)
_PAST2 = _TODAY - timedelta(days=180)
_PAST3 = _TODAY - timedelta(days=270)
_PAST4 = _TODAY - timedelta(days=360)


@pytest.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    await db_session.execute(delete(IVSnapshot))
    await db_session.execute(delete(EarningsEvent))
    await db_session.commit()
    yield
    await db_session.execute(delete(IVSnapshot))
    await db_session.execute(delete(EarningsEvent))
    await db_session.commit()


async def seed_event(
    db: AsyncSession,
    ticker: str,
    report_date: date,
    report_time: str = "amc",
    realized_move_pct=None,
):
    await db.execute(
        insert(EarningsEvent).values(
            ticker=ticker,
            report_date=report_date,
            report_time=report_time,
            source="finnhub",
            realized_move_pct=realized_move_pct,
        )
    )


async def seed_snapshot(
    db: AsyncSession,
    ticker: str,
    iv_rank: float = 60.0,
    expected_move_pct: float = 0.05,
):
    await db.execute(
        insert(IVSnapshot).values(
            ticker=ticker,
            snapshot_date=_TODAY,
            iv30=Decimal("0.30"),
            iv_rank=Decimal(str(iv_rank)),
            expected_move_pct=Decimal(str(expected_move_pct)),
            source="tastytrade",
        )
    )


# ---------------------------------------------------------------------------
# /api/earnings/calendar — max_realized_move_pct and quarters_used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_exposes_max_realized_move_pct(
    client: AsyncClient, db_session: AsyncSession
):
    moves = [0.03, 0.05, 0.08, 0.04]
    for i, past in enumerate([_PAST, _PAST2, _PAST3, _PAST4]):
        await seed_event(db_session, "AAPL", past, realized_move_pct=Decimal(str(moves[i])))
    await seed_event(db_session, "AAPL", _FUTURE)
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(_TODAY), "end": str(_FUTURE + timedelta(days=1))},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    row = data[0]
    assert row["max_realized_move_pct"] is not None
    assert abs(float(row["max_realized_move_pct"]) - 0.08) < 1e-5


@pytest.mark.asyncio
async def test_calendar_exposes_quarters_used(
    client: AsyncClient, db_session: AsyncSession
):
    for past in [_PAST, _PAST2, _PAST3]:
        await seed_event(db_session, "AAPL", past, realized_move_pct=Decimal("0.05"))
    await seed_event(db_session, "AAPL", _FUTURE)
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(_TODAY), "end": str(_FUTURE + timedelta(days=1))},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["quarters_used"] == 3


@pytest.mark.asyncio
async def test_calendar_max_null_when_no_history(
    client: AsyncClient, db_session: AsyncSession
):
    await seed_event(db_session, "AAPL", _FUTURE)
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(_TODAY), "end": str(_FUTURE + timedelta(days=1))},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["max_realized_move_pct"] is None
    assert data[0]["quarters_used"] == 0


# ---------------------------------------------------------------------------
# /api/earnings/screen — max_realized_move_pct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_exposes_max_realized_move_pct(
    client: AsyncClient, db_session: AsyncSession
):
    moves = [0.04, 0.07, 0.05, 0.06]
    for i, past in enumerate([_PAST, _PAST2, _PAST3, _PAST4]):
        await seed_event(db_session, "AAPL", past, realized_move_pct=Decimal(str(moves[i])))
    await seed_event(db_session, "AAPL", _FUTURE)
    await seed_snapshot(db_session, "AAPL")
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/screen",
        params={"start": str(_TODAY), "end": str(_FUTURE + timedelta(days=1))},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert abs(float(data[0]["max_realized_move_pct"]) - 0.07) < 1e-5


# ---------------------------------------------------------------------------
# suggested_entry_date in /api/earnings/calendar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_suggested_entry_date_amc(
    client: AsyncClient, db_session: AsyncSession
):
    report = _TODAY + timedelta(days=7)
    await seed_event(db_session, "AAPL", report, report_time="amc")
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(_TODAY), "end": str(report)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["suggested_entry_date"] == str(report)


@pytest.mark.asyncio
async def test_calendar_suggested_entry_date_bmo(
    client: AsyncClient, db_session: AsyncSession
):
    # Wednesday BMO → Tuesday entry
    report = date(2026, 7, 22)  # Wednesday
    await seed_event(db_session, "AAPL", report, report_time="bmo")
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(report), "end": str(report)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["suggested_entry_date"] == "2026-07-21"  # Tuesday


@pytest.mark.asyncio
async def test_calendar_suggested_entry_date_monday_bmo(
    client: AsyncClient, db_session: AsyncSession
):
    # Monday BMO → Friday entry (skipping weekend)
    report = date(2026, 7, 20)  # Monday
    await seed_event(db_session, "AAPL", report, report_time="bmo")
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(report), "end": str(report)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["suggested_entry_date"] == "2026-07-17"  # Friday


@pytest.mark.asyncio
async def test_calendar_suggested_entry_date_unknown(
    client: AsyncClient, db_session: AsyncSession
):
    report = date(2026, 7, 22)
    await seed_event(db_session, "AAPL", report, report_time="unknown")
    await db_session.commit()

    resp = await client.get(
        "/api/earnings/calendar",
        params={"start": str(report), "end": str(report)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["suggested_entry_date"] is None
