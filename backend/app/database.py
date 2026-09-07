import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Values of sslmode/ssl that mean "do not negotiate TLS".
_SSL_OFF = {"disable", "disabled", "false", "off", "0"}


def _get_ssl_context():
    """Create SSL context for Neon Postgres connection."""
    ssl_ctx = ssl.create_default_context()
    return ssl_ctx


def _split_ssl_option(url: str) -> tuple[str, bool]:
    """Return (url without ssl query params, whether TLS should be used).

    asyncpg's ``connect()`` takes ``ssl=``, not libpq's ``sslmode=``, and the
    SQLAlchemy asyncpg dialect does not translate between them — a URL
    carrying ``?sslmode=...`` reaches the driver as an unexpected keyword and
    raises TypeError. So the option is parsed out here and expressed only
    through ``connect_args``.

    TLS stays on unless the URL explicitly opts out. The opt-out exists for a
    plaintext Postgres on localhost — a CI service container or local
    docker-compose — where there is no certificate to verify against.
    Production points at Neon and is unaffected.
    """
    if not url:
        return url, False

    parts = urlsplit(url)
    kept: list[tuple[str, str]] = []
    use_tls = True
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in ("sslmode", "ssl"):
            if value.strip().lower() in _SSL_OFF:
                use_tls = False
            continue
        kept.append((key, value))

    cleaned = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return cleaned, use_tls


DATABASE_URL, _use_tls = _split_ssl_option(settings.neon_database_url)

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.environment == "development",
    pool_pre_ping=True,
    connect_args={"ssl": _get_ssl_context()} if _use_tls else {},
)


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
