"""Ticker symbol validation.

Symbols arrive as path parameters and are interpolated into upstream
market-data URLs. Unvalidated, a symbol containing '&' or '?' injects extra
query parameters into those requests (the host and API key are fixed, so
this is request shaping rather than SSRF or key disclosure) and pollutes the
per-symbol caches. Normalising at the boundary removes the class.
"""

import re

from fastapi import HTTPException

# Equities, ETFs, and the share-class suffixes FMP uses: BRK.B, RDS-A.
# Index symbols (^GSPC) are deliberately not allowed — nothing in the app
# requests them, so the narrower set is the safer one.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def normalize_ticker(raw: str) -> str:
    """Upper-case and validate a symbol, or raise 422."""
    candidate = (raw or "").strip().upper()
    if not _TICKER_RE.match(candidate):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid ticker symbol: {raw!r}",
        )
    return candidate
