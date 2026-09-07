"""Baseline response hardening.

The app previously returned no security headers at all: Basic Auth
credentials (base64, trivially decoded) were strippable on a first
plain-HTTP visit, and the terminal could be framed by any origin.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app, auth_throttle


@pytest.fixture(autouse=True)
def _reset_throttle():
    auth_throttle.reset()
    yield
    auth_throttle.reset()


@pytest_asyncio.fixture
async def bare_client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_headers_present_on_a_normal_response(bare_client):
    response = await bare_client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_headers_present_on_the_401(bare_client, monkeypatch):
    """A response the auth middleware returns itself must still be hardened."""
    monkeypatch.setattr(settings, "auth_username", "dane")
    monkeypatch.setattr(settings, "auth_password", "owner-pw")

    response = await bare_client.get("/api/me")
    assert response.status_code == 401
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_hsts_only_in_production(bare_client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    response = await bare_client.get("/api/health")
    assert "Strict-Transport-Security" not in response.headers

    monkeypatch.setattr(settings, "environment", "production")
    response = await bare_client.get("/api/health")
    assert response.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_docs_are_disabled_in_production():
    """The schema enumerates every route including /api/internal/*.

    ``app`` is built at import time from the environment the test process
    was started with, so assert the wiring rule rather than the live app.
    """
    from app.main import _docs_enabled

    assert _docs_enabled is not settings.is_production
