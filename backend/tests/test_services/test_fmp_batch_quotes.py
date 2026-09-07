"""
Tests for FMPClient.get_batch_quotes (bulk endpoint, chunking, symbol
mapping, fallback) and the scanner's Stage 1 cache coverage guard.

No network calls — `_request_with_retry` is replaced on the instance.
"""

from __future__ import annotations

import os

from app.services.fmp_client import FMPClient
from app.services.scanner import StockScanner


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def make_client(tmp_path) -> FMPClient:
    return FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp_cache"))


def quote_for(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "price": 50.0,
        "volume": 1_000_000,
        "marketCap": 5_000_000_000,
    }


def _requested_symbols(url: str) -> list[str]:
    return url.split("symbols=")[1].split("&")[0].split(",")


# ---------------------------------------------------------------------------
# get_batch_quotes
# ---------------------------------------------------------------------------


async def test_chunks_universe_into_bulk_requests(tmp_path):
    client = make_client(tmp_path)
    urls: list[str] = []

    async def fake_request(url, http_client, retries=None):
        urls.append(url)
        return FakeResponse([quote_for(s) for s in _requested_symbols(url)])

    client._request_with_retry = fake_request

    tickers = [f"TK{i}" for i in range(250)]
    quotes = await client.get_batch_quotes(tickers)

    assert len(urls) == 3  # 100 + 100 + 50
    assert all("/stable/batch-quote" in u for u in urls)
    assert len(_requested_symbols(urls[0])) == 100
    assert len(_requested_symbols(urls[2])) == 50
    assert len(quotes) == 250
    assert quotes["TK0"]["price"] == 50.0


async def test_dot_class_symbols_map_back_to_requested_spelling(tmp_path):
    client = make_client(tmp_path)

    async def fake_request(url, http_client, retries=None):
        # FMP returns dashed class symbols regardless of request spelling.
        return FakeResponse([quote_for("BRK-B"), quote_for("AAPL")])

    client._request_with_retry = fake_request

    quotes = await client.get_batch_quotes(["BRK.B", "AAPL"])

    assert set(quotes) == {"BRK.B", "AAPL"}
    assert quotes["BRK.B"]["symbol"] == "BRK-B"


async def test_failed_chunk_falls_back_to_individual_requests(tmp_path):
    client = make_client(tmp_path)
    urls: list[str] = []

    async def fake_request(url, http_client, retries=None):
        urls.append(url)
        if "batch-quote" in url:
            return None  # whole chunk fails
        symbol = url.split("symbol=")[1].split("&")[0]
        return FakeResponse([quote_for(symbol)])

    client._request_with_retry = fake_request

    quotes = await client.get_batch_quotes(["AAPL", "MSFT", "NVDA"])

    assert len(quotes) == 3
    batch_urls = [u for u in urls if "batch-quote" in u]
    single_urls = [u for u in urls if "/stable/quote?" in u]
    assert len(batch_urls) == 1
    assert len(single_urls) == 3


async def test_missing_symbols_are_dropped_not_invented(tmp_path):
    client = make_client(tmp_path)

    async def fake_request(url, http_client, retries=None):
        # Only half the requested symbols come back (e.g. delisted).
        symbols = _requested_symbols(url)
        return FakeResponse([quote_for(s) for s in symbols[::2]])

    client._request_with_retry = fake_request

    tickers = [f"TK{i}" for i in range(10)]
    quotes = await client.get_batch_quotes(tickers)

    assert len(quotes) == 5
    assert "TK1" not in quotes


# ---------------------------------------------------------------------------
# Scanner Stage 1 cache guard
# ---------------------------------------------------------------------------


class FakeFMPForScanner:
    """Returns quotes for a fixed subset of tickers; Stage 2 yields nothing."""

    def __init__(self, answered: set[str]):
        self.answered = answered

    async def get_batch_quotes(self, tickers, max_concurrent=10):
        return {t: quote_for(t) for t in tickers if t in self.answered}

    async def get_historical_data(self, ticker, days=252, use_cache=True):
        return None

    async def get_earnings_date(self, ticker):
        return None


def _stage1_cache_files(cache_dir: str) -> list[str]:
    return [f for f in os.listdir(cache_dir) if f.startswith("stage1_")]


async def test_partial_quote_coverage_skips_stage1_cache(tmp_path):
    tickers = [f"TK{i}" for i in range(10)]
    cache_dir = str(tmp_path / "scan_cache")
    scanner = StockScanner(
        cache_dir=cache_dir,
        client=FakeFMPForScanner(answered=set(tickers[:5])),  # 50% coverage
    )

    await scanner.run_scan(tickers, use_cache=True, universe="sp500")

    assert _stage1_cache_files(cache_dir) == []


async def test_full_quote_coverage_writes_stage1_cache(tmp_path):
    tickers = [f"TK{i}" for i in range(10)]
    cache_dir = str(tmp_path / "scan_cache")
    scanner = StockScanner(
        cache_dir=cache_dir,
        client=FakeFMPForScanner(answered=set(tickers)),
    )

    await scanner.run_scan(tickers, use_cache=True, universe="sp500")

    assert len(_stage1_cache_files(cache_dir)) == 1


# ---------------------------------------------------------------------------
# Chunk-failure diagnostics (#72)
#
# Every bulk chunk was failing in production and falling back to individual
# requests (~645 throttled calls, ~6.5 min). The HTTP status was logged at
# debug only — below the production log level — so the cause was invisible.
# ---------------------------------------------------------------------------


class FailingResponse:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


async def test_chunk_failure_logs_the_http_status(tmp_path, caplog):
    """The warning must name the status, or #72 stays undiagnosable."""
    import httpx

    client = make_client(tmp_path)

    async def fake_request(url, http_client, retries=None):
        if "batch-quote" in url:
            from app.services.fmp_client import RequestFailure
            client.last_failure = RequestFailure(402, "Payment Required")
            return None
        return FakeResponse([quote_for("AAPL")])

    client._request_with_retry = fake_request
    with caplog.at_level("WARNING"):
        await client.get_batch_quotes(["AAPL"])

    assert "402" in caplog.text
    assert "Payment Required" in caplog.text


async def test_permanent_failure_stops_retrying_the_bulk_endpoint(tmp_path):
    """A 4xx will not fix itself mid-run; don't pay a round trip per chunk."""
    from app.services.fmp_client import RequestFailure

    client = make_client(tmp_path)
    client.BATCH_QUOTE_CHUNK = 2
    batch_attempts = []

    async def fake_request(url, http_client, retries=None):
        if "batch-quote" in url:
            batch_attempts.append(url)
            client.last_failure = RequestFailure(403, "not on your plan")
            return None
        symbol = url.split("symbol=")[1].split("&")[0]
        return FakeResponse([quote_for(symbol)])

    client._request_with_retry = fake_request
    quotes = await client.get_batch_quotes(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])

    # Three chunks of two, but the bulk endpoint is tried only once.
    assert len(batch_attempts) == 1
    # Nothing is dropped — every symbol still resolves via the fallback.
    assert set(quotes) == {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}


async def test_rate_limiting_does_not_disable_the_bulk_endpoint(tmp_path):
    """429 is transient: later chunks must still try the bulk path."""
    from app.services.fmp_client import RequestFailure

    client = make_client(tmp_path)
    client.BATCH_QUOTE_CHUNK = 2
    batch_attempts = []

    async def fake_request(url, http_client, retries=None):
        if "batch-quote" in url:
            batch_attempts.append(url)
            client.last_failure = RequestFailure(429, "rate limited")
            return None
        symbol = url.split("symbol=")[1].split("&")[0]
        return FakeResponse([quote_for(symbol)])

    client._request_with_retry = fake_request
    await client.get_batch_quotes(["AAA", "BBB", "CCC", "DDD"])

    assert len(batch_attempts) == 2


async def test_non_json_200_is_a_failed_chunk_not_a_crash(tmp_path):
    """A 200 with an HTML body used to raise out of the whole scan."""
    client = make_client(tmp_path)

    async def fake_request(url, http_client, retries=None):
        if "batch-quote" in url:
            return FailingResponse(200, text="<html>maintenance</html>")
        symbol = url.split("symbol=")[1].split("&")[0]
        return FakeResponse([quote_for(symbol)])

    client._request_with_retry = fake_request
    quotes = await client.get_batch_quotes(["AAPL"])
    assert quotes["AAPL"]["symbol"] == "AAPL"


def test_request_failure_classification():
    from app.services.fmp_client import RequestFailure

    assert RequestFailure(403, "nope").is_permanent() is True
    assert RequestFailure(400).is_permanent() is True
    assert RequestFailure(429, "slow down").is_permanent() is False
    assert RequestFailure(500).is_permanent() is False
    assert RequestFailure(None, "timeout").is_permanent() is False
    assert "network error" in RequestFailure(None, "timeout").describe()
    assert RequestFailure(402, "Payment Required").describe() == "HTTP 402 Payment Required"
