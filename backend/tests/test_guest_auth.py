"""Guest-role gating in BasicAuthMiddleware.

Guests may reach the scanner and earnings market data; everything
personal (watchlist, portfolio, journal, alerts, dashboard, earnings
trades) is owner-only. These tests exercise the middleware directly —
no database access is needed because guest-denied requests are rejected
before any route runs. The exception is the per-ticker earnings test at
the bottom, which seeds a trade to prove a guest-*allowed* route does not
leak owner data in its response body.
"""

import base64
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app, auth_throttle
from app.models.earnings import EarningsEvent
from app.models.earnings_trades import EarningsTrade
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


async def test_guest_universe_listing_never_calls_fmp(auth_client, monkeypatch, tmp_path):
    """Since #118 ``combined`` is built from FMP's screener. Listing must
    only peek at the cached build: the guest-visible route can't be a way
    to trigger paid calls."""
    from app.routes import scanner as scanner_routes
    from app.services import screener_universe
    from app.services.screener_universe import UniverseCache

    monkeypatch.setattr(
        scanner_routes, "universe_cache", UniverseCache(str(tmp_path / "universe.json"))
    )

    async def boom(*args, **kwargs):
        raise AssertionError("universe listing must not resolve via FMP")

    monkeypatch.setattr(screener_universe, "resolve_universe", boom)
    monkeypatch.setattr(scanner_routes, "resolve_universe", boom)

    response = await auth_client.get("/api/scan/universes", headers=GUEST)
    assert response.status_code == 200
    combined = next(u for u in response.json() if u["name"] == "combined")
    assert combined["size"] is None  # no build on this instance yet


# --- Gemini narrative is owner-only (paid call per new ticker/day) ---------


class _CountingGemini:
    def __init__(self):
        self.calls = 0

    async def generate_text(self, prompt: str) -> str:
        self.calls += 1
        return "Generated narrative."


class _ContextMarket:
    def __init__(self):
        self.context_calls = 0

    async def ticker_context(self, ticker: str) -> dict:
        self.context_calls += 1
        return {"ticker": ticker, "company_name": ticker, "headlines": []}


@pytest.fixture
def narrative_env(monkeypatch):
    from app.routes.ticker import _narrative_cache

    gemini = _CountingGemini()
    market = _ContextMarket()
    monkeypatch.setattr(app.state, "vision_parser", gemini, raising=False)
    _narrative_cache.clear()
    yield gemini, market
    _narrative_cache.clear()


async def test_guest_narrative_degrades_without_calling_gemini(
    auth_client, narrative_env
):
    gemini, market = narrative_env
    app.dependency_overrides[get_market] = lambda: market

    response = await auth_client.get("/api/ticker/ZZZT/context/narrative", headers=GUEST)

    assert response.status_code == 200
    assert response.json() == {
        "ticker": "ZZZT",
        "narrative": None,
        "narrative_available": False,
    }
    assert gemini.calls == 0
    assert market.context_calls == 0


async def test_owner_narrative_generates_and_guest_reads_cache(
    auth_client, narrative_env
):
    gemini, market = narrative_env
    app.dependency_overrides[get_market] = lambda: market

    owner = await auth_client.get("/api/ticker/ZZZT/context/narrative", headers=OWNER)
    assert owner.status_code == 200
    assert owner.json()["narrative"] == "Generated narrative."
    assert gemini.calls == 1

    # The owner already paid for today's narrative; a guest may read it free.
    guest = await auth_client.get("/api/ticker/ZZZT/context/narrative", headers=GUEST)
    assert guest.status_code == 200
    assert guest.json()["narrative"] == "Generated narrative."
    assert guest.json()["narrative_available"] is True
    assert gemini.calls == 1

# --- Response-body leaks on guest-allowed routes ----------------------------
#
# The middleware allowlist is path-based, so it cannot see what a permitted
# route returns. GET /api/earnings/{ticker} is guest-reachable via the
# /api/earnings prefix but used to embed the owner's recent earnings trades.

_LEAK_TICKER = "GSTLK"


async def _cleanup_leak_ticker(db: AsyncSession) -> None:
    # Trades reference earnings_events via RESTRICT — delete trades first.
    await db.execute(delete(EarningsTrade).where(EarningsTrade.ticker == _LEAK_TICKER))
    await db.execute(delete(EarningsEvent).where(EarningsEvent.ticker == _LEAK_TICKER))
    await db.commit()


@pytest_asyncio.fixture
async def seeded_trade(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    """Owner + guest credentials, plus one earnings trade on _LEAK_TICKER.

    Uses the conftest ``client`` (test DB session override) rather than
    ``auth_client``, then enables auth by setting credentials — the
    middleware reads settings per request.
    """
    monkeypatch.setattr(settings, "auth_username", "dane")
    monkeypatch.setattr(settings, "auth_password", "owner-pw")
    monkeypatch.setattr(settings, "guest_username", "friend")
    monkeypatch.setattr(settings, "guest_password", "guest-pw")

    await _cleanup_leak_ticker(db_session)
    today = date.today()
    event_id = (
        await db_session.execute(
            insert(EarningsEvent)
            .values(
                ticker=_LEAK_TICKER,
                report_date=today + timedelta(days=7),
                report_time="amc",
                source="finnhub",
            )
            .returning(EarningsEvent.id)
        )
    ).scalar_one()
    await db_session.execute(
        insert(EarningsTrade).values(
            ticker=_LEAK_TICKER,
            earnings_event_id=event_id,
            structure="iron_condor",
            is_paper=False,
            entry_date=today,
            expiry_date=today + timedelta(days=10),
            short_put_strike=Decimal("190.00"),
            long_put_strike=Decimal("185.00"),
            short_call_strike=Decimal("210.00"),
            long_call_strike=Decimal("215.00"),
            entry_credit=Decimal("1.50"),
            contracts=1,
            commissions=Decimal("2.00"),
            adjustments=[],
            status="open",
        )
    )
    await db_session.commit()
    yield client
    await _cleanup_leak_ticker(db_session)


async def test_guest_ticker_earnings_hides_owner_trades(seeded_trade):
    response = await seeded_trade.get(f"/api/earnings/{_LEAK_TICKER}", headers=GUEST)
    assert response.status_code == 200
    body = response.json()
    assert body["recent_trades"] == []
    # The rest of the payload is still served to guests.
    assert body["ticker"] == _LEAK_TICKER
    assert body["event"] is not None


async def test_owner_ticker_earnings_still_includes_trades(seeded_trade):
    response = await seeded_trade.get(f"/api/earnings/{_LEAK_TICKER}", headers=OWNER)
    assert response.status_code == 200
    trades = response.json()["recent_trades"]
    assert len(trades) == 1
    assert trades[0]["ticker"] == _LEAK_TICKER
