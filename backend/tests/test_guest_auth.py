"""Guest-role gating in BasicAuthMiddleware.

Guests may reach the scanner and earnings market data; everything
personal (watchlist, portfolio, journal, alerts, dashboard, earnings
trades) is owner-only. These tests exercise the middleware directly —
no database access is needed because guest-denied requests are rejected
before any route runs.
"""

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.services.market import get_market


def basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


OWNER = basic("dane", "owner-pw")
GUEST = basic("friend", "guest-pw")


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """Client with owner + guest credentials configured."""
    monkeypatch.setattr(settings, "auth_username", "dane")
    monkeypatch.setattr(settings, "auth_password", "owner-pw")
    monkeypatch.setattr(settings, "guest_username", "friend")
    monkeypatch.setattr(settings, "guest_password", "guest-pw")

    class _NullMarket:
        pass

    app.dependency_overrides[get_market] = lambda: _NullMarket()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_no_credentials_rejected(auth_client):
    response = await auth_client.get("/api/me")
    assert response.status_code == 401


async def test_owner_role(auth_client):
    response = await auth_client.get("/api/me", headers=OWNER)
    assert response.status_code == 200
    assert response.json() == {"role": "owner"}


async def test_guest_role(auth_client):
    response = await auth_client.get("/api/me", headers=GUEST)
    assert response.status_code == 200
    assert response.json() == {"role": "guest"}


async def test_guest_denied_personal_routes(auth_client):
    for path in (
        "/api/watchlist",
        "/api/portfolio/positions",
        "/api/journal",
        "/api/alerts",
        "/api/dashboard/summary",
        "/api/earnings/trades",
        "/api/trade-campaigns",
    ):
        response = await auth_client.get(path, headers=GUEST)
        assert response.status_code == 403, path
        assert response.json()["detail"] == "This section requires owner access"


async def test_guest_allowed_scanner_and_earnings(auth_client):
    for path in ("/api/scan/universes", "/api/earnings/calendar"):
        response = await auth_client.get(path, headers=GUEST)
        assert response.status_code not in (401, 403), path


async def test_guest_wrong_password_rejected(auth_client):
    response = await auth_client.get("/api/me", headers=basic("friend", "nope"))
    assert response.status_code == 401


async def test_guest_login_rejected_when_guest_not_configured(
    auth_client, monkeypatch
):
    monkeypatch.setattr(settings, "guest_username", "")
    monkeypatch.setattr(settings, "guest_password", "")
    response = await auth_client.get("/api/me", headers=GUEST)
    assert response.status_code == 401
