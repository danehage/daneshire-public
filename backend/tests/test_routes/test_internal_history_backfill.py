"""
Tests for ``POST /api/internal/earnings/backfill-history`` (#66).

Verify auth, Stage-1 target scoping (tags + gate), chunked window
coverage, ticker filtering before upsert, idempotency (including
``realized_move_pct`` preservation), and failed-chunk skip-and-count.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.earnings import EarningsEvent
from app.models.watchlist import WatchlistItem
from app.schemas.market import PriceQuote
from app.services.market import EarningsDateUnknown, UpstreamError, get_market


TEST_SECRET = "test-scheduler-secret"


@pytest.fixture(autouse=True)
async def setup_scheduler_secret():
    original = settings.scheduler_secret
    settings.scheduler_secret = TEST_SECRET
    yield
    settings.scheduler_secret = original


@pytest.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    for model in (WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()
    yield
    for model in (WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()
    app.dependency_overrides.pop(get_market, None)


def auth_headers():
    return {"X-Scheduler-Secret": TEST_SECRET}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeMarket:
    """Serves canned Finnhub-shaped calendar events and gate quotes.

    Records every ``earnings_calendar`` call; the first ``fail_chunks``
    calls raise :class:`EarningsDateUnknown`.
    """

    def __init__(self, calendar=None, quotes=None, fail_chunks: int = 0):
        self.calendar_events = calendar or []
        self.quote_responses = quotes or {}
        self.fail_chunks = fail_chunks
        self.calls: list[tuple[date, date]] = []

    async def earnings_calendar(self, start: date, end: date):
        self.calls.append((start, end))
        if len(self.calls) <= self.fail_chunks:
            raise EarningsDateUnknown("finnhub down (fake)")
        return [
            e
            for e in self.calendar_events
            if start <= date.fromisoformat(e["date"]) <= end
        ]

    async def quotes(self, tickers):
        out = {}
        for t in tickers:
            t = t.upper()
            value = self.quote_responses.get(t)
            out[t] = value if value is not None else UpstreamError(t, "no quote in fake")
        return out


def _override_market(**kwargs) -> _FakeMarket:
    fake = _FakeMarket(**kwargs)
    app.dependency_overrides[get_market] = lambda: fake
    return fake


def _raw_event(ticker: str, days_ago: int, hour: str = "amc") -> dict:
    d = date.today() - timedelta(days=days_ago)
    return {
        "symbol": ticker,
        "date": d.isoformat(),
        "hour": hour,
        "year": d.year,
        "quarter": (d.month - 1) // 3 + 1,
    }


async def _tag_ticker(db: AsyncSession, ticker: str):
    db.add(WatchlistItem(ticker=ticker.upper(), tags=["earnings-candidate"]))
    await db.commit()


# ---------------------------------------------------------------------------
# Auth + no targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_scheduler_secret(client: AsyncClient):
    response = await client.post("/api/internal/earnings/backfill-history")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_no_targets_makes_no_finnhub_calls(client: AsyncClient):
    fake = _override_market(calendar=[_raw_event("AAPL", 100)])

    response = await client.post(
        "/api/internal/earnings/backfill-history", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_tickers"] == 0
    assert body["chunks_total"] == 0
    assert body["events_upserted"] == 0
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Target scoping + filtering + chunk coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upserts_only_target_tickers(client: AsyncClient, db_session: AsyncSession):
    await _tag_ticker(db_session, "AAPL")
    fake = _override_market(
        calendar=[
            _raw_event("AAPL", 100),
            _raw_event("AAPL", 190),
            _raw_event("NOISE", 100),  # not a target — must be dropped
        ]
    )

    response = await client.post(
        "/api/internal/earnings/backfill-history", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_tickers"] == 1
    assert body["events_upserted"] == 2
    assert body["chunks_failed"] == 0

    rows = (await db_session.execute(select(EarningsEvent))).scalars().all()
    assert sorted({r.ticker for r in rows}) == ["AAPL"]
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_chunks_cover_window_contiguously(client: AsyncClient, db_session: AsyncSession):
    await _tag_ticker(db_session, "AAPL")
    fake = _override_market(calendar=[])

    response = await client.post(
        "/api/internal/earnings/backfill-history?quarters=8", headers=auth_headers()
    )
    body = response.json()

    today = date.today()
    expected_start = today - timedelta(days=8 * 92)
    expected_end = today - timedelta(days=1)
    assert body["start"] == expected_start.isoformat()
    assert body["end"] == expected_end.isoformat()
    assert body["chunks_total"] == len(fake.calls)
    assert body["chunks_empty"] == body["chunks_total"]

    # Contiguous, ordered, within bounds, each chunk <= 30 days.
    assert fake.calls[0][0] == expected_start
    assert fake.calls[-1][1] == expected_end
    for start, end in fake.calls:
        assert (end - start).days <= 29
    for (_, prev_end), (next_start, _) in zip(fake.calls, fake.calls[1:]):
        assert next_start == prev_end + timedelta(days=1)


@pytest.mark.asyncio
async def test_gate_passed_ticker_is_a_target(client: AsyncClient, db_session: AsyncSession):
    """A liquid ticker with an upcoming event (no tag) gets history too."""
    db_session.add(
        EarningsEvent(
            ticker="NVDA",
            report_date=date.today() + timedelta(days=3),
            report_time="amc",
        )
    )
    await db_session.commit()

    _override_market(
        calendar=[_raw_event("NVDA", 95)],
        quotes={
            "NVDA": PriceQuote(
                ticker="NVDA",
                price=500.0,
                volume=5_000_000,
                avg_volume=30_000_000,
                market_cap=3_000_000_000_000,
            )
        },
    )

    response = await client.post(
        "/api/internal/earnings/backfill-history", headers=auth_headers()
    )
    body = response.json()
    assert body["target_tickers"] == 1
    assert body["events_upserted"] == 1

    past = (
        await db_session.execute(
            select(EarningsEvent).where(EarningsEvent.report_date < date.today())
        )
    ).scalars().all()
    assert [r.ticker for r in past] == ["NVDA"]


# ---------------------------------------------------------------------------
# Idempotency + failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_preserves_realized_move(
    client: AsyncClient, db_session: AsyncSession
):
    await _tag_ticker(db_session, "AAPL")
    event = _raw_event("AAPL", 100)

    # Pre-existing row for the same (ticker, date) with a computed move.
    db_session.add(
        EarningsEvent(
            ticker="AAPL",
            report_date=date.fromisoformat(event["date"]),
            report_time="bmo",
            realized_move_pct=Decimal("0.042"),
        )
    )
    await db_session.commit()

    _override_market(calendar=[event])

    for _ in range(2):
        response = await client.post(
            "/api/internal/earnings/backfill-history", headers=auth_headers()
        )
        assert response.status_code == 200

    rows = (await db_session.execute(select(EarningsEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert Decimal(row.realized_move_pct) == Decimal("0.042")
    # Conflict update refreshed the report_time from the (fake) provider.
    assert row.report_time == "amc"


@pytest.mark.asyncio
async def test_failed_chunk_is_skipped_and_counted(
    client: AsyncClient, db_session: AsyncSession
):
    await _tag_ticker(db_session, "AAPL")
    # Event ~2 months back would land in a late chunk; first chunk fails.
    fake = _override_market(calendar=[_raw_event("AAPL", 60)], fail_chunks=1)

    response = await client.post(
        "/api/internal/earnings/backfill-history", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["chunks_failed"] == 1
    assert body["chunks_total"] == len(fake.calls)
    assert body["events_upserted"] == 1

    rows = (await db_session.execute(select(EarningsEvent))).scalars().all()
    assert [r.ticker for r in rows] == ["AAPL"]
