"""Date-window cap on GET /api/earnings/calendar and /screen.

Both routes are guest-reachable; an unbounded window serialises the whole
earnings table (and /screen with min_volume bulk-quotes every ticker in it).
DB-free: the session and market are stubs that fail loudly if a rejected
request ever reaches them, and the in-range case uses a stub session that
returns no rows.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.routes.earnings import MAX_RANGE_DAYS
from app.services.market import get_market


class _EmptyResult:
    def all(self):
        return []


class _StubSession:
    def __init__(self):
        self.executed = 0

    async def execute(self, *_args, **_kwargs):
        self.executed += 1
        return _EmptyResult()


class _NoQuotesMarket:
    async def quotes(self, tickers):  # pragma: no cover - must not be reached
        raise AssertionError("rejected range must not hit the quote API")


@pytest_asyncio.fixture
async def range_client():
    session = _StubSession()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_market] = lambda: _NoQuotesMarket()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, session
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/earnings/calendar", "/api/earnings/screen"])
async def test_huge_range_rejected_before_querying(range_client, path):
    client, session = range_client
    res = await client.get(
        path,
        params={"start": "1900-01-01", "end": "2100-01-01", "min_volume": 1},
    )
    assert res.status_code == 422
    assert "at most" in res.json()["detail"]
    assert session.executed == 0


@pytest.mark.parametrize("path", ["/api/earnings/calendar", "/api/earnings/screen"])
async def test_far_start_with_default_end_rejected(range_client, path):
    """Omitting ``end`` (defaults to today+28d) doesn't bypass the cap."""
    client, session = range_client
    res = await client.get(path, params={"start": "1900-01-01"})
    assert res.status_code == 422
    assert session.executed == 0


@pytest.mark.parametrize("path", ["/api/earnings/calendar", "/api/earnings/screen"])
async def test_range_at_cap_allowed(range_client, path):
    client, session = range_client
    start = date(2026, 6, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS)
    res = await client.get(
        path, params={"start": start.isoformat(), "end": end.isoformat()}
    )
    assert res.status_code == 200
    assert res.json() == []
    assert session.executed == 1


@pytest.mark.parametrize("path", ["/api/earnings/calendar", "/api/earnings/screen"])
async def test_defaults_allowed(range_client, path):
    client, _ = range_client
    res = await client.get(path)
    assert res.status_code == 200


def test_cap_covers_ui_window():
    """The Earnings page asks for the next 10 trading days (~14 calendar
    days); the API default is 28. Both must stay well under the cap."""
    assert MAX_RANGE_DAYS >= 28
