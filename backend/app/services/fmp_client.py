"""
Financial Modeling Prep (FMP) API Client - Async version
"""

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from urllib.parse import parse_qs, quote as urlquote, urlsplit
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# Day-scoped disk cache files end in ``_<YYYY-MM-DD>.json`` (``stage1_*`` from
# the scanner, per-ticker history files from this client — matched on the
# suffix, not the prefix, so renamed cache kinds are still covered).
_DATED_CACHE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.json$")


def prune_dated_cache_files(cache_dir: str, today: Optional[date] = None) -> int:
    """Delete ``*_<date>.json`` files in ``cache_dir`` dated before yesterday.

    These caches are only ever read for the current day, but nothing deleted
    old ones. On Cloud Run the filesystem is in-memory and counts against the
    instance's 512Mi, so a long-lived instance accumulated every day's files.

    Yesterday is kept on purpose: writers date their files in different
    zones (the Stage-1 scan cache uses the ET date, this client's history
    cache the server's UTC date). Between 20:00 and 24:00 ET the UTC date is
    already tomorrow, so a strict "before today" cut deleted the scan cache
    the moment it was written. One extra day on disk is cheap.

    One ``os.scandir`` pass; never raises (pruning is best-effort). Returns
    the number of files removed.
    """
    cutoff_str = ((today or datetime.now().date()) - timedelta(days=1)).isoformat()
    removed = 0
    try:
        with os.scandir(cache_dir) as it:
            for entry in it:
                m = _DATED_CACHE_RE.search(entry.name)
                # ISO dates compare correctly as strings.
                if m is None or m.group(1) >= cutoff_str:
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        os.remove(entry.path)
                        removed += 1
                except OSError as e:
                    logger.debug("cache prune: could not remove %s: %s", entry.path, e)
    except OSError as e:
        logger.debug("cache prune: could not scan %s: %s", cache_dir, e)
    if removed:
        logger.info("cache prune: removed %d stale file(s) from %s", removed, cache_dir)
    return removed


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

    def is_no_data(self) -> bool:
        """A per-symbol "nothing here" answer rather than an outage.

        Logged at debug: an unknown ticker is expected noise, whereas
        401/403/429/5xx and network errors mean every call is failing.
        """
        return self.status == 404


class FMPRequestError(Exception):
    """An FMP request failed for a reason other than "no data for symbol".

    Raised (rather than returning ``None``) where the caller must tell an
    outage apart from an unknown ticker — see ``get_company_profile``.
    """

    def __init__(self, failure: RequestFailure, endpoint: str = ""):
        self.failure = failure
        where = f"{endpoint}: " if endpoint else ""
        super().__init__(f"FMP {where}{failure.describe()}")


def _num(v) -> Optional[float]:
    """Float or None; FMP sends numbers, nulls and the odd string."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _describe_request(url: str) -> str:
    """Path + symbol(s) of an FMP URL, never the query string.

    Every FMP URL carries ``apikey=`` in its query, so URLs must not be
    logged verbatim.
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    symbol = (query.get("symbol") or query.get("symbols") or [""])[0]
    if len(symbol) > 60:
        symbol = symbol[:60] + "..."
    return f"{parts.path} symbol={symbol}" if symbol else parts.path


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
        prune_dated_cache_files(cache_dir)

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
                        body = self._redact(response.text[:200])
                    except Exception:  # pragma: no cover - defensive
                        pass
                    failure = RequestFailure(response.status_code, body)
                    self.last_failure = failure
                    # A 404 for one symbol is expected noise; anything else
                    # (401/403 bad key, 402 plan, 5xx) means FMP is failing
                    # for every call and must reach Cloud Logging, which
                    # only receives INFO and above.
                    log = logger.debug if failure.is_no_data() else logger.warning
                    log(
                        "FMP request failed (%s): HTTP %d %s",
                        _describe_request(url),
                        response.status_code,
                        body,
                    )
                    return None

            except httpx.RequestError as e:
                detail = self._redact(str(e)[:200]) or type(e).__name__
                self.last_failure = RequestFailure(None, detail)
                if attempt < retries:
                    logger.debug(
                        "FMP network error (%s), retrying: %s",
                        _describe_request(url),
                        detail,
                    )
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    logger.warning(
                        "FMP network error (%s) after %d retries: %s",
                        _describe_request(url),
                        retries,
                        detail,
                    )
                    return None

        return None

    def _redact(self, text: str) -> str:
        """Strip the API key from text headed for the logs."""
        if self.api_key and text:
            return text.replace(self.api_key, "***")
        return text

    def _get_hist_cache_path(self, ticker: str) -> str:
        """Get path for today's historical data cache.

        The file holds the *full* series FMP returned (oldest -> newest), so
        any ``days`` can be served from it. The ``histfull_`` prefix keeps
        it from ever reading an older ``hist_`` file, which held only the
        first caller's ``days`` worth of bars.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.cache_dir, f"histfull_{ticker}_{today}.json")

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
                elif response.status_code != 404:
                    logger.warning(
                        "FMP request failed (/stable/quote symbol=%s): HTTP %d %s",
                        ticker,
                        response.status_code,
                        self._redact(response.text[:200]),
                    )

                return None

        except httpx.RequestError as e:
            logger.warning(
                "FMP network error (/stable/quote symbol=%s): %s",
                ticker,
                self._redact(str(e)[:200]) or type(e).__name__,
            )
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

        Returns at most ``days`` bars, always the *newest* ones (the list
        ends at the most recent bar).

        Uses daily caching since historical data doesn't change intraday.
        The cache holds the full series FMP returned regardless of ``days``,
        so callers asking for different windows (252 / 700 / 1500) all get
        a correct slice from the same file. A ticker with less history than
        ``days`` simply returns everything there is.
        """
        # Check cache first
        if use_cache:
            cached = self._load_hist_cache(ticker)
            if cached:
                return cached[-days:]

        try:
            url = f"{self.BASE_URL}/stable/historical-price-eod/full?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await self._request_with_retry(url, client)

                if response and response.status_code == 200:
                    data = response.json()
                    hist = None

                    # API returns list directly or wrapped in 'historical'
                    # key, newest-first. Cache the full series oldest-first.
                    if isinstance(data, list) and len(data) > 0:
                        hist = list(reversed(data))
                    elif isinstance(data, dict) and "historical" in data:
                        hist = list(reversed(data["historical"]))

                    if hist:
                        self._save_hist_cache(ticker, hist)
                        return hist[-days:]

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

        Returns ``None`` when FMP has no profile for the symbol, and raises
        :class:`FMPRequestError` when the request itself failed (bad key,
        5xx, network) so the ticker page can answer 502 instead of a
        misleading "ticker not found" 404.
        """
        try:
            url = f"{self.BASE_URL}/stable/profile?symbol={urlquote(ticker, safe='')}&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await self._request_with_retry(url, client)
                # Read last_failure before any further await: it is shared
                # client state and concurrent calls overwrite it.
                failure = self.last_failure
                if response is None and failure is not None and not failure.is_no_data():
                    raise FMPRequestError(failure, "/stable/profile")
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

    STATEMENT_KINDS = ("income-statement", "balance-sheet-statement", "cash-flow-statement")

    async def get_quarterly_statements(
        self, kind: str, ticker: str, limit: int = 4
    ) -> Optional[list[dict]]:
        """Fetch quarterly statements of one ``kind`` (most recent first).

        ``kind`` is one of ``STATEMENT_KINDS`` — the /stable/ path segment.
        """
        if kind not in self.STATEMENT_KINDS:
            raise ValueError(f"unknown statement kind: {kind}")
        try:
            url = (
                f"{self.BASE_URL}/stable/{kind}"
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
            logger.debug(f"Error fetching {kind} for {ticker}: {e}")
            return None

    async def get_income_statements(
        self, ticker: str, limit: int = 4
    ) -> Optional[list[dict]]:
        """Fetch quarterly income statements (most recent first)."""
        return await self.get_quarterly_statements("income-statement", ticker, limit)

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
        info = await self.get_earnings_info(ticker)
        if not info or info.get("next_date") is None:
            return None
        return {"date": info["next_date"], "days_until": info["days_until"]}

    async def get_earnings_info(self, ticker: str) -> Optional[dict]:
        """
        Next and last earnings report from one ``/stable/earnings`` call.

        Returns ``{"next_date", "days_until", "last_date", "last_eps_actual",
        "last_eps_estimate"}`` (any value may be None), or None when the
        request fails or FMP has no entries. "Last" is the newest entry on
        or before today that carries an actual EPS, i.e. a report that has
        happened. The same call the scanner always made for the next date,
        so the last-report facts (#117) cost no extra request.
        """
        try:
            # /stable/earnings replaces the legacy-gated /api/v3/earning_calendar
            # (dead on this plan since the 2025-08-31 cutoff — every call
            # returned an error payload, so earnings dates were silently None).
            url = f"{self.BASE_URL}/stable/earnings?symbol={urlquote(ticker, safe='')}&limit=8&apikey={self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)

                if response.status_code == 429:
                    logger.warning(f"Rate limited on earnings for {ticker}")
                    return None
                if response.status_code != 200:
                    return None
                data = response.json()
                if not isinstance(data, list) or not data:
                    return None

                today = date.today()
                # Entries come date-descending (farthest future first), so
                # collect upcoming dates and take the nearest explicitly.
                upcoming: list[date] = []
                last: Optional[tuple[date, dict]] = None
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
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
                    if earnings_date <= today and entry.get("epsActual") is not None:
                        if last is None or earnings_date > last[0]:
                            last = (earnings_date, entry)

                nearest = min(upcoming) if upcoming else None
                return {
                    "next_date": nearest.isoformat() if nearest else None,
                    "days_until": (nearest - today).days if nearest else None,
                    "last_date": last[0].isoformat() if last else None,
                    "last_eps_actual": _num(last[1].get("epsActual")) if last else None,
                    "last_eps_estimate": _num(last[1].get("epsEstimated")) if last else None,
                }

        except httpx.RequestError as e:
            logger.debug(f"Error fetching earnings for {ticker}: {e}")
            return None
