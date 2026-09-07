import asyncio
import base64
import binascii
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import get_db


class FailureThrottle:
    """Backoff over recent failed credential attempts.

    Deliberately NOT keyed on client IP. The previous implementation
    bucketed failures by ``request.client.host`` and checked the bucket
    *before* validating credentials, which made it either a self-DoS (all
    callers share one bucket behind a proxy, so five anonymous requests
    locked the owner out of the app for five minutes) or useless (where
    X-Forwarded-For is trusted, an attacker rotates the header and never
    trips it). Both were verified against a local instance.

    Correct credentials are checked before this is consulted and are never
    delayed, so there is no lockout an attacker can trigger against the
    owner; only failures are slowed.
    """

    WINDOW_SECONDS = 300
    FREE_ATTEMPTS = 5           # attempts before backoff kicks in
    BASE_SECONDS = 0.5
    MAX_SECONDS = 5.0
    MAX_TRACKED = 1000          # hard cap so the list cannot grow unbounded

    def __init__(self) -> None:
        self._failures: list[float] = []

    def reset(self) -> None:
        """Drop recorded failures. Used by tests."""
        self._failures.clear()

    def record_failure(self) -> None:
        if len(self._failures) >= self.MAX_TRACKED:
            self._failures.pop(0)
        self._failures.append(time.monotonic())

    def recent_failures(self) -> int:
        """Prune expired failures and return how many remain in the window."""
        now = time.monotonic()
        self._failures = [
            t for t in self._failures if now - t < self.WINDOW_SECONDS
        ]
        return len(self._failures)

    def backoff_seconds(self) -> float:
        """Delay to apply to the failed attempt just recorded."""
        excess = self.recent_failures() - self.FREE_ATTEMPTS
        if excess <= 0:
            return 0.0
        return min(self.BASE_SECONDS * (2 ** (excess - 1)), self.MAX_SECONDS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline response hardening.

    The app previously returned no security headers at all, so: Basic Auth
    credentials (base64, trivially decoded) were strippable on a first
    plain-HTTP visit, and the terminal could be framed by any origin.

    The CSP is deliberately narrow. The Vite production build emits
    content-hashed JS/CSS under /assets and no inline <script>, so
    ``script-src 'self'`` holds. ``style-src`` allows inline styles because
    React sets element styles directly (recharts sizing, transient
    drag-and-drop transforms from @dnd-kit).
    """

    CSP = "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        )
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Content-Security-Policy", self.CSP)
        # Cloud Run terminates TLS; only assert HSTS where the deployment is
        # actually HTTPS, so local http:// development is unaffected.
        if settings.is_production:
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


# One throttle per process. Tests reset it via ``auth_throttle.reset()``.
auth_throttle = FailureThrottle()


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth middleware for protecting the app."""

    # Paths that don't require auth (health checks for Cloud Run)
    PUBLIC_PATHS = {"/api/health"}
    # Paths that use scheduler secret instead of basic auth
    INTERNAL_PREFIX = "/api/internal/"

    # API surface reachable with guest credentials: read-only scanner +
    # earnings market data and the ticker context block (guests reach the
    # ticker detail page from scan results). Everything else under /api/
    # is owner-only. Non-/api paths (the static frontend) are always
    # reachable once authenticated.
    #
    # NOTE: starting a scan is deliberately NOT reachable as a guest. Each
    # POST /api/scan/execute fans out to ~900 paid FMP requests and retains
    # the results in memory — see GUEST_DENIED_API_PREFIXES.
    GUEST_ALLOWED_API_PREFIXES = ("/api/me", "/api/scan", "/api/earnings", "/api/ticker")
    GUEST_DENIED_API_PREFIXES = ("/api/earnings/trades", "/api/scan/execute")

    def _resolve_role(self, username: str, password: str) -> str | None:
        """Map credentials to a role, or None if they match nothing.

        Both credential pairs are always compared to keep timing uniform.
        ``compare_digest`` raises TypeError on non-ASCII str input, so the
        comparison is done over UTF-8 bytes instead.
        """

        def _eq(a: str, b: str) -> bool:
            return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

        owner_user = _eq(username, settings.auth_username)
        owner_pass = _eq(password, settings.auth_password)
        guest_user = _eq(username, settings.guest_username)
        guest_pass = _eq(password, settings.guest_password)

        if owner_user and owner_pass:
            return "owner"
        if settings.guest_enabled and guest_user and guest_pass:
            return "guest"
        return None

    def _is_guest_allowed(self, path: str) -> bool:
        if not path.startswith("/api/"):
            return True
        if path.startswith(self.GUEST_DENIED_API_PREFIXES):
            return False
        return path.startswith(self.GUEST_ALLOWED_API_PREFIXES)

    @staticmethod
    def _credentials_from(request: Request) -> tuple[str, str] | None:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
        return username, password

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for public health endpoint
        if path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth for internal endpoints (they use X-Scheduler-Secret)
        if path.startswith(self.INTERNAL_PREFIX):
            return await call_next(request)

        # Skip if auth not configured (local development)
        if not settings.auth_enabled:
            request.state.role = "owner"
            return await call_next(request)

        # Validate credentials FIRST. A caller presenting correct credentials
        # is never delayed or refused, however many failures preceded them —
        # that is what stops an attacker from locking the owner out.
        credentials = self._credentials_from(request)
        if credentials is not None:
            role = self._resolve_role(*credentials)
            if role == "guest" and not self._is_guest_allowed(path):
                # An authenticated guest on an owner-only path is a routing
                # mistake, not a credential guess: no backoff.
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "This section requires owner access"},
                )
            if role is not None:
                request.state.role = role
                return await call_next(request)

        # Bad or absent credentials: slow the guess down, then 401.
        auth_throttle.record_failure()
        delay = auth_throttle.backoff_seconds()
        if delay:
            await asyncio.sleep(delay)
        return Response(
            content="Authentication required",
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="Danecast Trades"'},
        )


# Static files directory (frontend build)
STATIC_DIR = Path(__file__).parent.parent / "static"

from app.routes import (
    watchlist_router,
    price_targets_router,
    journal_router,
    scanner_router,
    alerts_router,
    internal_router,
    dashboard_router,
    portfolio_router,
    earnings_router,
    ticker_router,
    trade_campaigns_router,
)
from app.services.fmp_client import FMPClient
from app.services.finnhub_client import FinnhubClient
from app.services.market import MarketData
from app.services.scanner import StockScanner
from app.services.tastytrade_client import TastytradeClient
from app.services.vision_parser import GeminiVisionParser


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: build one shared FMPClient, one StockScanner that consumes
    # it, and one MarketData seam over both. Routes resolve the singleton
    # via Depends(get_market). See CONTEXT.md § Market Data.
    fmp = FMPClient()
    scanner = StockScanner(client=fmp)
    # FinnhubClient is optional — app starts cleanly without the key; the
    # earnings endpoints will return 502 until the key is configured.
    try:
        finnhub = FinnhubClient()
    except ValueError:
        finnhub = None
    # TastytradeClient is optional — app starts cleanly without the
    # OAuth credentials; the IV refresh endpoint will return 502 until
    # TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN are provisioned.
    try:
        tastytrade = TastytradeClient()
    except ValueError:
        tastytrade = None
    app.state.market = MarketData(fmp, scanner, finnhub, tastytrade)
    # GeminiVisionParser is optional — app starts cleanly without the key or
    # when the library is unavailable; /snapshots/parse returns 503 until configured.
    try:
        app.state.vision_parser = GeminiVisionParser()
    except (ValueError, ImportError):
        app.state.vision_parser = None
    yield
    # Shutdown
    if tastytrade is not None:
        await tastytrade.aclose()


# The OpenAPI schema enumerates every route, including /api/internal/*, and
# was readable with guest credentials. Serve it only outside production.
_docs_enabled = not settings.is_production

app = FastAPI(
    title="Danecast Trades API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Basic auth middleware (production security)
app.add_middleware(BasicAuthMiddleware)

# Added last => outermost layer, so security headers are attached to every
# response including the 401/403 the auth middleware returns itself.
app.add_middleware(SecurityHeadersMiddleware)

# CORS: Only needed for local development (cross-origin requests from Vite dev server)
# In production, frontend is served from same origin, so CORS is not needed
if settings.environment == "development":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # Vite dev server
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(watchlist_router)
app.include_router(price_targets_router)
app.include_router(journal_router)
app.include_router(scanner_router)
app.include_router(alerts_router)
app.include_router(internal_router)
app.include_router(dashboard_router)
app.include_router(portfolio_router)
app.include_router(earnings_router)
app.include_router(ticker_router)
app.include_router(trade_campaigns_router)


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/me")
async def whoami(request: Request):
    """Role of the authenticated caller — the frontend uses this to hide
    owner-only sections from guests."""
    return {"role": getattr(request.state, "role", "owner")}


@app.get("/api/health/db")
async def health_check_db(db: AsyncSession = Depends(get_db)):
    """Database health check. Protected by basic auth (not in PUBLIC_PATHS)."""
    result = await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


# Serve static frontend in production
if STATIC_DIR.exists():
    # Mount static assets (JS, CSS, etc.) if directory exists
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # Catch-all route for client-side routing (must be last)
    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        # API routes are handled by their routers - if we get here for /api/*, it's a 404
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        # Serve index.html for all other routes (SPA client-side routing).
        # no-cache forces revalidation on every load — without it, browsers
        # heuristically cache index.html (10% of Last-Modified age) and keep
        # loading the previous deploy's hashed bundles for hours after a fix
        # ships. The bundles under /assets are content-hashed, so they stay
        # safely cacheable.
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path, headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=404, detail="Frontend not built")
