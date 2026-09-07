"""
Financial Modeling Prep (FMP) API Client - Async version
"""

import asyncio
import json
import logging
import os
from datetime import datetime, date
from typing import Optional

from urllib.parse import quote as urlquote
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestFailure:
    """Why the last request failed, for actionable logging."""

    status: Optional[int]
    body: str = ""

    def describe(self) -> str:
        if self.status is None:
            return f"network error: {self.body}"
        detail = f" {self.body}" if self.body else ""
        return f"HTTP {self.status}{detail}"

    def is_permanent(self) -> bool:
        """A client error that will not resolve by retrying this run."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


class FMPClient:
    """Async client for Financial Modeling Prep API with rate limit handling."""

    BASE_URL = "https://financialmodelingprep.com"

    # Rate limiting: FMP allows ~300/min on free tier, ~750/min on starter
    # Plan confirmed at 300 calls/min (2026-07-08); 4.5/s = 270/min headroom
    REQUESTS_PER_SECOND = 4.5
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0  # seconds

    def __init__(self, api_key: str = None, cache_dir: str = "scanner_cache"):
        self.api_key = api_key or settings.fmp_api_key
        if not self.api_key:
            raise ValueError("FMP_API_KEY not provided")

        self.cache_dir = cache_dir
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        # Why the most recent request failed, for actionable logging (#72).
        self.last_failure: Optional[RequestFailure] = None

        # Throttling: track last request time
        self._last_request_time = 0.0
        self._request_lock = asyncio.Lock()

    async def _throttle(self):
        """Ensure we don't exceed rate limits."""
        async with self._request_lock:
            now = asyncio.get_event_loop().time()
            min_interval = 1.0 / self.REQUESTS_PER_SECOND
            elapsed = now - self._last_request_time
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()

    async def _request_with_retry(
        self, url: str, client: httpx.AsyncClient, retries: int = None
    ) -> Optional[httpx.Response]:
        """Make a request with throttling and retry on rate limit.

        On a non-retryable failure the status is recorded in
        ``last_failure`` so callers can report *why* a request failed. It
        used to be logged at debug only, which is below the production log
        level — issue #72 could not be diagnosed from prod logs because the
        status code never appeared anywhere.
        """
        retries = retries if retries is not None else self.MAX_RETRIES
        self.last_failure = None

        for attempt in range(retries + 1):
            await self._throttle()

            try:
                response = await client.get(url)

                if response.status_code == 200:
                    return response
                elif response.status_code == 429:
                    if attempt < retries:
                        delay = self.RETRY_DELAY * (2 ** attempt)  # exponential backoff
                        logger.warning(f"Rate limited, retrying in {delay}s (attempt {attempt + 1}/{retries})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Rate limited after {retries} retries")
                        self.last_failure = RequestFailure(429, "rate limited")
                        return None
                else:
                    body = ""
                    try:
                        body = response.text[:200]
                    except Exception:  # pragma: no cover - defensive
                        pass
                    self.last_failure = RequestFailure(response.status_code, body)
                    logger.debug(
                        f"Request failed with status {response.status_code}: {body}"
                    )
                    return None

            except httpx.RequestError as e:
                logger.debug(f"Request error: {e}")
                self.last_failure = RequestFailure(None, str(e)[:200])
                if attempt < retries:
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    return None

        return None

    def _get_hist_cache_path(self, ticker: str) -> str:
        """Get path for today's historical data cache."""
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.cache_dir, f"hist_{ticker}_{today}.json")

    def _load_hist_cache(self, ticker: str) -> Optional[list[dict]]:
        """Load cached historical data if it exists for today."""
        cache_path = self._get_hist_cache_path(ticker)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def _save_hist_cache(self, ticker: str, data: list[dict]):
        """Save historical data to cache."""
        cache_path = self._get_hist_cache_path(ticker)
        try:
            with open(cache_path, "w") as f:
                json.dump(data, f)
        except IOError as e:
            logger.debug(f"Failed to cache {ticker} history: {e}")

    async def get_quote(self, ticker: str) -> Optional[dict]:
        """Fetch a single stock quote."""
        try:
            url = f"{self.BASE_URL}/stable/quote?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)

                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0]
                    elif isinstance(data, dict) and data:
                        return data
                elif response.status_code == 429:
                    logger.warning(f"Rate limited on {ticker}")

                return None

        except httpx.RequestError as e:
            logger.debug(f"Error fetching quote for {ticker}: {e}")
            return None

    BATCH_QUOTE_CHUNK = 100

    async def get_batch_quotes(
        self, tickers: list[str], max_concurrent: int = 10
    ) -> dict[str, dict]:
        """
        Fetch quotes via FMP's bulk endpoint, BATCH_QUOTE_CHUNK symbols per
        request, with throttling and 429 retry. Returns dict mapping the
        *requested* ticker -> quote data.

        FMP returns dot-class symbols dashed (BRK.B -> BRK-B), so responses
        are matched back to the requested spelling. If a whole chunk fails
        (or the bulk endpoint is unavailable on the plan), falls back to
        throttled individual requests for that chunk so symbols are never
        silently dropped wholesale.
        """
        all_quotes: dict[str, dict] = {}
        total = len(tickers)
        # Cleared when the bulk endpoint reports a permanent failure, so the
        # remaining chunks skip it instead of retrying a known-bad call.
        batch_supported = True

        logger.info(
            f"Fetching quotes for {total} stocks "
            f"({self.BATCH_QUOTE_CHUNK} per request)..."
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            for i in range(0, total, self.BATCH_QUOTE_CHUNK):
                chunk = tickers[i : i + self.BATCH_QUOTE_CHUNK]
                symbols = ",".join(chunk)
                url = (
                    f"{self.BASE_URL}/stable/batch-quote"
                    f"?symbols={urlquote(symbols, safe=',')}&apikey={self.api_key}"
                )
                response = None
                if batch_supported:
                    response = await self._request_with_retry(url, client)

                items = []
                if response is not None:
                    try:
                        data = response.json()
                    except ValueError:
                        # 200 with a non-JSON body: treat as a failed chunk
                        # rather than raising out of the whole scan.
                        data = None
                        self.last_failure = RequestFailure(200, "non-JSON body")
                    if isinstance(data, list):
                        items = data

                if items:
                    by_symbol = {
                        item["symbol"]: item for item in items if item.get("symbol")
                    }
                    for ticker in chunk:
                        quote = by_symbol.get(ticker) or by_symbol.get(
                            ticker.replace(".", "-")
                        )
                        if quote:
                            all_quotes[ticker] = quote
                else:
                    failure = self.last_failure
                    if batch_supported:
                        logger.warning(
                            "Batch quote chunk failed (%d symbols; %s); "
                            "falling back to individual requests",
                            len(chunk),
                            failure.describe() if failure else "no response",
                        )
                        # A 4xx that is not rate limiting means the endpoint
                        # is not going to start working mid-run (wrong plan,
                        # wrong parameter, bad key). Stop paying a failed
                        # round trip per chunk and go straight to singles.
                        if failure is not None and failure.is_permanent():
                            batch_supported = False
                            logger.warning(
                                "Disabling the bulk quote endpoint for this "
                                "run after %s; using individual requests.",
                                failure.describe(),
                            )
                    for ticker in chunk:
                        single_url = (
                            f"{self.BASE_URL}/stable/quote"
                            f"?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
                        )
                        single = await self._request_with_retry(single_url, client)
                        if single is not None:
                            data = single.json()
                            if isinstance(data, list) and len(data) > 0:
                                all_quotes[ticker] = data[0]
                            elif isinstance(data, dict) and data:
                                all_quotes[ticker] = data

        missing = total - len(all_quotes)
        logger.info(
            f"Completed: {len(all_quotes)}/{total} quotes collected"
            + (f" ({missing} missing)" if missing else "")
        )
        return all_quotes

    async def get_historical_data(
        self, ticker: str, days: int = 252, use_cache: bool = True
    ) -> Optional[list[dict]]:
        """
        Fetch historical price data for a ticker.
        Returns list of daily OHLCV data, oldest to newest.

        Uses daily caching since historical data doesn't change intraday.
        """
        # Check cache first
        if use_cache:
            cached = self._load_hist_cache(ticker)
            if cached:
                return cached[:days] if len(cached) > days else cached

        try:
            url = f"{self.BASE_URL}/stable/historical-price-eod/full?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await self._request_with_retry(url, client)

                if response and response.status_code == 200:
                    data = response.json()
                    hist = None

                    # API returns list directly or wrapped in 'historical' key
                    if isinstance(data, list) and len(data) > 0:
                        hist = list(reversed(data[:days]))
                    elif isinstance(data, dict) and "historical" in data:
                        hist = list(reversed(data["historical"][:days]))

                    if hist:
                        self._save_hist_cache(ticker, hist)
                        return hist

                return None

        except (KeyError, ValueError) as e:
            logger.debug(f"Data parsing error for {ticker}: {e}")
            return None

    async def get_historical_batch(
        self, tickers: list[str], days: int = 252, max_concurrent: int = 5
    ) -> dict[str, list[dict]]:
        """
        Fetch historical data for multiple tickers with concurrency limit.
        Returns dict mapping ticker -> historical data.
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        results = {}

        async def fetch_one(ticker: str):
            async with semaphore:
                data = await self.get_historical_data(ticker, days)
                if data:
                    results[ticker] = data

        await asyncio.gather(*[fetch_one(t) for t in tickers], return_exceptions=True)
        return results

    async def get_sp500_constituents(self) -> list[str]:
        """
        Fetch current S&P 500 constituent symbols from FMP.
        Returns list of ticker symbols.
        """
        try:
            url = f"{self.BASE_URL}/api/v3/sp500_constituent?apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)

                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        tickers = [item.get("symbol") for item in data if item.get("symbol")]
                        logger.info(f"Fetched {len(tickers)} S&P 500 constituents from FMP")
                        return tickers

                elif response.status_code == 429:
                    logger.warning("Rate limited fetching S&P 500 constituents")

                return []

        except httpx.RequestError as e:
            logger.error(f"Error fetching S&P 500 constituents: {e}")
            return []

    async def get_company_profile(self, ticker: str) -> Optional[dict]:
        """Fetch company profile: name, sector, description, 52w range.

        Uses the /stable/ API — the /api/v3/ endpoints are legacy-gated
        (subscriptions before 2025-08-31 only) and return an error payload
        on this plan.
        """
        try:
            url = f"{self.BASE_URL}/stable/profile?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._request_with_retry(url, client)
                if response:
                    data = response.json()
                    if isinstance(data, list) and data:
                        return data[0]
            return None
        except httpx.RequestError as e:
            logger.debug(f"Error fetching profile for {ticker}: {e}")
            return None

    async def get_ratios_ttm(self, ticker: str) -> Optional[dict]:
        """Fetch TTM ratios: netProfitMarginTTM, debtToEquityRatioTTM.

        Note: /stable/key-metrics-ttm no longer carries margin or D/E —
        those fields moved to /stable/ratios-ttm.
        """
        try:
            url = f"{self.BASE_URL}/stable/ratios-ttm?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._request_with_retry(url, client)
                if response:
                    data = response.json()
                    if isinstance(data, list) and data:
                        return data[0]
            return None
        except httpx.RequestError as e:
            logger.debug(f"Error fetching TTM ratios for {ticker}: {e}")
            return None

    async def get_income_statements(
        self, ticker: str, limit: int = 4
    ) -> Optional[list[dict]]:
        """Fetch quarterly income statements (most recent first)."""
        try:
            url = (
                f"{self.BASE_URL}/stable/income-statement"
                f"?symbol={urlquote(ticker, safe='')}&period=quarter&limit={limit}&apikey={self.api_key}"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._request_with_retry(url, client)
                if response:
                    data = response.json()
                    if isinstance(data, list):
                        return data[:limit]
            return None
        except httpx.RequestError as e:
            logger.debug(f"Error fetching income statements for {ticker}: {e}")
            return None

    async def get_stock_news(
        self, ticker: str, limit: int = 5
    ) -> Optional[list[dict]]:
        """Fetch recent news headlines for a ticker."""
        try:
            url = (
                f"{self.BASE_URL}/stable/news/stock"
                f"?symbols={urlquote(ticker, safe='')}&limit={limit}&apikey={self.api_key}"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._request_with_retry(url, client)
                if response:
                    data = response.json()
                    if isinstance(data, list):
                        return data[:limit]
            return None
        except httpx.RequestError as e:
            logger.debug(f"Error fetching news for {ticker}: {e}")
            return None

    async def get_earnings_date(self, ticker: str) -> Optional[dict]:
        """
        Fetch upcoming earnings date for a ticker.
        Returns dict with 'date' and 'days_until' or None if not found.
        """
        try:
            # /stable/earnings replaces the legacy-gated /api/v3/earning_calendar
            # (dead on this plan since the 2025-08-31 cutoff — every call
            # returned an error payload, so earnings dates were silently None).
            url = f"{self.BASE_URL}/stable/earnings?symbol={urlquote(ticker, safe='')}&limit=8&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)

                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list) and len(data) > 0:
                        today = date.today()
                        # Entries come date-descending (farthest future first),
                        # so collect all upcoming dates and take the nearest.
                        upcoming: list[date] = []
                        for entry in data:
                            earnings_date_str = entry.get("date")
                            if not earnings_date_str:
                                continue
                            try:
                                earnings_date = datetime.strptime(
                                    earnings_date_str, "%Y-%m-%d"
                                ).date()
                            except ValueError:
                                continue
                            if (earnings_date - today).days >= 0:
                                upcoming.append(earnings_date)
                        if upcoming:
                            nearest = min(upcoming)
                            return {
                                "date": nearest.isoformat(),
                                "days_until": (nearest - today).days,
                            }

                elif response.status_code == 429:
                    logger.warning(f"Rate limited on earnings for {ticker}")

                return None

        except httpx.RequestError as e:
            logger.debug(f"Error fetching earnings for {ticker}: {e}")
            return None
