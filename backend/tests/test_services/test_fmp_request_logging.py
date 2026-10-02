"""
FMP failures must reach Cloud Logging (INFO+) without leaking the key.

``_request_with_retry`` used to log non-200s and network errors at debug,
below the production log level, so a revoked key was invisible. These run
against ``httpx.MockTransport`` — no network.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.services.fmp_client import FMPClient, FMPRequestError

API_KEY = "sekret-fmp-key"


def _client(tmp_path) -> FMPClient:
    client = FMPClient(api_key=API_KEY, cache_dir=str(tmp_path / "fmp_cache"))
    client.RETRY_DELAY = 0
    return client


def _http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _url(path: str, symbol: str = "AAPL") -> str:
    return f"{FMPClient.BASE_URL}{path}?symbol={symbol}&apikey={API_KEY}"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 402, 500, 503])
async def test_outage_statuses_log_a_warning_without_the_key(tmp_path, caplog, status):
    client = _client(tmp_path)

    def handler(request):
        return httpx.Response(status, text=f'{{"Error Message": "nope {API_KEY}"}}')

    with caplog.at_level(logging.DEBUG, logger="app.services.fmp_client"):
        async with _http(handler) as http:
            assert await client._request_with_retry(_url("/stable/quote"), http) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"HTTP {status}" in message
    assert "/stable/quote symbol=AAPL" in message
    assert API_KEY not in caplog.text
    assert "apikey" not in caplog.text
    assert client.last_failure.status == status


@pytest.mark.asyncio
async def test_per_symbol_404_stays_at_debug(tmp_path, caplog):
    client = _client(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="app.services.fmp_client"):
        async with _http(lambda r: httpx.Response(404, text="not found")) as http:
            assert await client._request_with_retry(_url("/stable/quote"), http) is None

    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


@pytest.mark.asyncio
async def test_network_error_warns_once_after_retries(tmp_path, caplog):
    client = _client(tmp_path)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    with caplog.at_level(logging.DEBUG, logger="app.services.fmp_client"):
        async with _http(handler) as http:
            result = await client._request_with_retry(_url("/stable/quote"), http, retries=2)

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "network error" in warnings[0].getMessage()
    assert API_KEY not in caplog.text
    assert client.last_failure.status is None


@pytest.mark.asyncio
async def test_company_profile_raises_on_request_failure(tmp_path, monkeypatch):
    """A revoked key must not read as "no profile" (→ ticker not found)."""
    client = _client(tmp_path)

    async def failing(url, http, retries=None):
        from app.services.fmp_client import RequestFailure

        client.last_failure = RequestFailure(401, "Invalid API KEY")
        return None

    monkeypatch.setattr(client, "_request_with_retry", failing)
    with pytest.raises(FMPRequestError, match="401"):
        await client.get_company_profile("AAPL")


@pytest.mark.asyncio
async def test_company_profile_returns_none_for_unknown_symbol(tmp_path, monkeypatch):
    client = _client(tmp_path)

    class _Empty:
        status_code = 200

        def json(self):
            return []

    async def empty(url, http, retries=None):
        client.last_failure = None
        return _Empty()

    monkeypatch.setattr(client, "_request_with_retry", empty)
    assert await client.get_company_profile("ZZZZZZZ") is None
