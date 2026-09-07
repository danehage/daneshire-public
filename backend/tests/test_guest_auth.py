"""Guest-role gating in BasicAuthMiddleware.

Guests may reach the scanner and earnings market data; everything
personal (watchlist, portfolio, journal, alerts, dashboard, earnings
trades) is owner-only. These tests exercise the middleware directly —
no database access is needed because guest-denied requests are rejected
before any route runs.
"""

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app, auth_throttle
from app.services.market import get_market


def basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


OWNER = basic("dane", "owner-pw")
GUEST = basic("friend", "guest-pw")


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Failed-credential backoff is process-global; keep tests isolated."""
    auth_throttle.reset()
    yield
    auth_throttle.reset()


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


# --- Failed-credential backoff ---------------------------------------------
#
# Regression tests for the pre-launch audit finding: the throttle used to be
# keyed on client IP and checked *before* credentials were validated, so five
# anonymous 401s locked the owner out of the app with a 429 for five minutes.


async def test_correct_credentials_never_blocked_by_prior_failures(
    auth_client, monkeypatch
):
    """The owner is not locked out by someone else's failed guesses."""
    # Zero the delay: this test asserts the access-control property, not the
    # timing. The backoff curve itself is covered by the unit test below.
    monkeypatch.setattr(auth_throttle, "BASE_SECONDS", 0.0)
    monkeypatch.setattr(auth_throttle, "MAX_SECONDS", 0.0)

    for _ in range(20):
        bad = await auth_client.get("/api/me", headers=basic("x", "y"))
        assert bad.status_code == 401

    owner = await auth_client.get("/api/me", headers=OWNER)
    assert owner.status_code == 200
    assert owner.json() == {"role": "owner"}

    guest = await auth_client.get("/api/me", headers=GUEST)
    assert guest.status_code == 200
    assert guest.json() == {"role": "guest"}


def test_backoff_grows_with_failures_then_caps():
    auth_throttle.reset()
    for _ in range(auth_throttle.FREE_ATTEMPTS):
        auth_throttle.record_failure()
        assert auth_throttle.backoff_seconds() == 0.0

    auth_throttle.record_failure()
    assert auth_throttle.backoff_seconds() == auth_throttle.BASE_SECONDS

    for _ in range(20):
        auth_throttle.record_failure()
    assert auth_throttle.backoff_seconds() == auth_throttle.MAX_SECONDS


def test_failure_list_is_bounded():
    auth_throttle.reset()
    for _ in range(auth_throttle.MAX_TRACKED + 50):
        auth_throttle.record_failure()
    assert auth_throttle.recent_failures() <= auth_throttle.MAX_TRACKED


async def test_guest_403_does_not_count_as_a_failed_guess(auth_client):
    """A guest browsing to an owner-only page must not slow down logins."""
    for _ in range(10):
        response = await auth_client.get("/api/watchlist", headers=GUEST)
        assert response.status_code == 403
    assert auth_throttle.recent_failures() == 0


async def test_non_ascii_credentials_do_not_crash(auth_client):
    """compare_digest raises TypeError on non-ASCII str; we compare bytes."""
    response = await auth_client.get("/api/me", headers=basic("dan\u00e9", "p\u00e4ssw\u00f6rd"))
    assert response.status_code == 401


async def test_malformed_basic_header_rejected(auth_client):
    for header in (
        {"Authorization": "Basic !!!not-base64!!!"},
        {"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()},
        {"Authorization": "Bearer sometoken"},
    ):
        response = await auth_client.get("/api/me", headers=header)
        assert response.status_code == 401


async def test_guest_cannot_start_a_scan(auth_client):
    """POST /api/scan/execute fans out to ~900 paid FMP calls."""
    response = await auth_client.post(
        "/api/scan/execute", headers=GUEST, json={"universe": "combined"}
    )
    assert response.status_code == 403


async def test_guest_can_still_read_scan_metadata(auth_client):
    response = await auth_client.get("/api/scan/universes", headers=GUEST)
    assert response.status_code not in (401, 403)
