import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    neon_database_url: str = ""

    # Market data
    fmp_api_key: str = ""
    finnhub_api_key: str = ""
    tastytrade_client_secret: str = ""
    tastytrade_refresh_token: str = ""

    # Earnings ironfly funnel — Stage-1 liquidity gate (#65).
    # Tickers with upcoming earnings must clear all three thresholds to get
    # nightly IV snapshots; the `earnings-candidate` tag bypasses the gate.
    earnings_gate_min_price: float = 20.0
    earnings_gate_min_avg_volume: int = 1_000_000
    earnings_gate_min_market_cap: int = 2_000_000_000

    # Vision parsing
    gemini_api_key: str = ""

    # Notifications
    pushover_user_key: str = ""
    pushover_api_token: str = ""

    # Backup
    gcs_backup_bucket: str = ""

    # Internal auth
    scheduler_secret: str = ""

    # Basic auth for web access
    auth_username: str = ""
    auth_password: str = ""

    # Optional guest credentials — read-only access to the scanner and
    # earnings market data, no personal data (watchlist/portfolio/journal/alerts)
    guest_username: str = ""
    guest_password: str = ""

    # Environment
    environment: str = "development"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password)

    @property
    def guest_enabled(self) -> bool:
        return bool(self.guest_username and self.guest_password)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()


# Settings that must be non-empty in production. With any of them blank the
# app fails open: no AUTH_* makes BasicAuthMiddleware grant owner to every
# request, and no SCHEDULER_SECRET leaves /api/internal/* unprotected.
_REQUIRED_IN_PRODUCTION = ("auth_username", "auth_password", "scheduler_secret")


def validate_startup_config(cfg: Settings | None = None) -> None:
    """Refuse to start with a configuration that would fail open.

    Called from the FastAPI lifespan. Raises ``RuntimeError`` naming the
    offending setting; never includes setting values in the message.

    - In production, AUTH_USERNAME, AUTH_PASSWORD and SCHEDULER_SECRET must
      all be set.
    - On Cloud Run (``K_SERVICE`` is set by the platform), ENVIRONMENT must
      be "production". The "development" default enables the scheduler
      secret bypass, /docs and dev CORS, and the service is public
      (--allow-unauthenticated).
    """
    cfg = cfg or settings

    if os.environ.get("K_SERVICE") and not cfg.is_production:
        raise RuntimeError(
            "Refusing to start: running on Cloud Run (K_SERVICE is set) but "
            "ENVIRONMENT is not 'production'."
        )

    if cfg.is_production:
        missing = [
            name.upper() for name in _REQUIRED_IN_PRODUCTION if not getattr(cfg, name)
        ]
        if missing:
            raise RuntimeError(
                "Refusing to start in production: required setting(s) empty: "
                + ", ".join(missing)
            )
