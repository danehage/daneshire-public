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
