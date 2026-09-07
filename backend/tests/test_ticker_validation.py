"""Ticker symbols reach upstream market-data URLs by interpolation.

Unvalidated, a symbol containing '&' injects extra query parameters into
those requests and pollutes the per-symbol caches. The host and API key are
fixed, so this was request shaping rather than SSRF or key disclosure — but
it is a class worth removing at the boundary.
"""

import pytest
from fastapi import HTTPException

from app.schemas.tickers import normalize_ticker


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("aapl", "AAPL"),
        ("  msft  ", "MSFT"),
        ("BRK.B", "BRK.B"),
        ("RDS-A", "RDS-A"),
    ],
)
def test_valid_symbols_normalize(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "AAPL&apikey=stolen",       # query-parameter injection
        "AAPL?limit=99999",
        "AAPL#fragment",
        "AAPL/../../etc/passwd",
        "AAPL AAPL",
        "",
        "   ",
        "A" * 20,                    # over length
        "<script>",
        "^GSPC",   # index symbols are not requested by this app
    ],
)
def test_hostile_symbols_rejected(raw):
    with pytest.raises(HTTPException) as excinfo:
        normalize_ticker(raw)
    assert excinfo.value.status_code == 422


def test_url_interpolation_is_encoded():
    """Even if a hostile symbol got past the boundary, the client escapes it."""
    from urllib.parse import quote as urlquote

    assert urlquote("AAPL&apikey=x", safe="") == "AAPL%26apikey%3Dx"
