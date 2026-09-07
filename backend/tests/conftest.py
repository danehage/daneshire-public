from typing import AsyncGenerator
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database import _get_ssl_context, _split_ssl_option
from app.main import app, auth_throttle
from app.database import get_db
from app.config import settings
from app.services.market import get_market

# --- Production database guard ---------------------------------------------
# On 2026-06-11 this suite wiped the production database: a stale .env pointed
# NEON_DATABASE_URL at the prod endpoint, and the cleanup fixtures DELETE from
# nearly every table. Refuse to run against prod outright. If the production
# endpoint is ever recreated under a new ID, update it here.
_PROD_ENDPOINT_ID = "ep-billowing-dew-amz8z9f3"

_db_host = urlsplit(settings.neon_database_url.replace("+asyncpg", "")).hostname or ""
if _PROD_ENDPOINT_ID in _db_host or settings.is_production:
    pytest.exit(
        "REFUSING TO RUN: NEON_DATABASE_URL points at the PRODUCTION database "
        f"({_db_host or 'unset'}). The test fixtures delete data. Point "
        "backend/.env at the Neon 'tests' branch before running pytest.",
        returncode=3,
    )


class _NullMarket:
    """Default ``MarketData`` stand-in for tests.

    The FastAPI ``lifespan`` startup that constructs the real
    ``MarketData`` does not run under ``ASGITransport``, so any route
    that depends on ``get_market`` would otherwise raise. Tests that
    actually exercise market behaviour override ``get_market`` themselves
    with a richer fake; this default just ensures unrelated tests don't
    explode.
    """

    async def analyses(self, tickers):
        return {}

    async def quotes(self, tickers):
        return {}

    async def analysis(self, ticker):
        raise RuntimeError("test forgot to override get_market")

    async def quote(self, ticker):
        raise RuntimeError("test forgot to override get_market")

    async def history(self, ticker, days=252):
        raise RuntimeError("test forgot to override get_market")

    async def realized_move_history(self, ticker, events, quarters=8):
        return [None] * len(events)


# --- Auth neutralisation (issue #64) ----------------------------------------
#
# BasicAuthMiddleware is skipped entirely when no credentials are configured,
# so the route tests were written to call endpoints unauthenticated. A
# developer .env with AUTH_USERNAME/AUTH_PASSWORD set therefore turned the
# whole route suite red with 401s — and, once failed attempts started
# incurring backoff, slow as well (one test file went from 0.6s to 33s).
#
# Settings are process-wide, so blank the credentials for every test. Tests
# that exercise the middleware on purpose (test_guest_auth.py,
# test_security_headers.py) set them again in their own fixtures, which run
# after this one.
@pytest.fixture(autouse=True)
def _disable_auth_by_default(monkeypatch):
    monkeypatch.setattr(settings, "auth_username", "")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "guest_username", "")
    monkeypatch.setattr(settings, "guest_password", "")
    auth_throttle.reset()
    yield
    auth_throttle.reset()


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for testing against a real Postgres."""
    # Same URL/TLS handling as the app, so a plaintext local or CI Postgres
    # works via ?sslmode=disable and a remote one still gets a verifying
    # context. (This fixture used to build its own CERT_NONE context.)
    url, use_tls = _split_ssl_option(settings.neon_database_url)
    engine = create_async_engine(
        url,
        connect_args={"ssl": _get_ssl_context()} if use_tls else {},
    )

    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as session:
        yield session
        # Rollback any uncommitted changes
        await session.rollback()

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create a test client with database session override."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # Default market override — individual tests may replace this with a
    # richer fake by reassigning ``app.dependency_overrides[get_market]``.
    app.dependency_overrides[get_market] = lambda: _NullMarket()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
