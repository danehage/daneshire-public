"""
MarketData — single in-process seam for all FMP traffic.

Owns one `FMPClient` (shared throttle + retry, unchanged) and one
`StockScanner` (delegated to for analysis). Adds:

* TTL cache keyed by (kind, ticker)
* Per-key single-flight via `asyncio.Future` so concurrent identical
  requests share one in-flight fetch rather than stampeding FMP
* Batch methods that surface per-ticker failures via
  `dict[ticker, T | MarketDataError]` — whole-batch failures still raise

Constructed once in FastAPI's `lifespan` (see `app.main`) and stashed on
`app.state.market`. Routes consume it via `Depends(get_market)`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, Optional, TypeVar

from fastapi import Request

from app.schemas.iv import IVSnapshotRaw
from app.schemas.market import HistoryBar, HistoryFrame, PriceQuote, TechnicalAnalysis
from app.services.fmp_client import FMPClient, RequestFailure
from app.services.scanner import StockScanner
from app.services.tastytrade_client import (
    TastytradeRateLimited,
    TastytradeStaleQuote,
    TastytradeUnavailable,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class MarketDataError(Exception):
    """Base class for all `MarketData` failures.

    Raised from singleton methods (`quote`, `analysis`, `history`).
    Returned as values inside the per-ticker dicts produced by batch
    methods (`quotes`, `analyses`), so callers can distinguish "ticker
    failed" from "request failed".
    """

    def __init__(self, ticker: str, message: str = ""):
        self.ticker = ticker
        super().__init__(message or f"{type(self).__name__}({ticker})")


class TickerNotFound(MarketDataError):
    """FMP returned no data for this ticker (delisted, typo, off-market)."""


class RateLimited(MarketDataError):
    """FMP rate limit exhausted even after the throttle's own retries."""


class UpstreamError(MarketDataError):
    """Unexpected FMP error (network, malformed payload, etc.)."""


class EarningsDateUnknown(MarketDataError):
    """Finnhub returned no earnings calendar data for the requested range."""

    def __init__(self, message: str = ""):
        super().__init__("", message or "EarningsDateUnknown")


class OptionsDataUnavailable(MarketDataError):
    """Tastytrade has no usable options data for this ticker today.

    Causes: missing chain, missing underlying quote, no front-week
    expiry, stale/zero straddle quotes, or no remember-token provisioned.

    ``reason`` buckets the cause for refresh summaries (#77): one of
    ``stale_quote``, ``rate_limited``, ``no_data``, ``not_configured``,
    ``error``.
    """

    def __init__(self, ticker: str, message: str = "", reason: str = "no_data"):
        super().__init__(ticker, message)
        self.reason = reason


# ---------------------------------------------------------------------------
# TTL cache + single-flight primitive
# ---------------------------------------------------------------------------


T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class _TTLSingleflightCache(Generic[T]):
    """Tiny stdlib-only TTL cache with per-key single-flight.

    Single-flight is implemented via a dict of in-flight `asyncio.Future`s
    keyed by the cache key: when key K is being fetched, additional
    callers `await` the same future instead of triggering parallel
    requests. The first caller is responsible for fulfilling the future
    (success or exception) and removing it from the dict.
    """

    __slots__ = ("_ttl", "_clock", "_entries", "_inflight", "_lock")

    def __init__(self, ttl_seconds: float, clock=time.monotonic):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._entries: dict[Any, _CacheEntry[T]] = {}
        self._inflight: dict[Any, asyncio.Future[T]] = {}
        # Guards entries + inflight dict membership. Held only for O(1)
        # bookkeeping, never across the actual fetch.
        self._lock = asyncio.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl

    def _get_fresh(self, key: Any) -> Optional[T]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            # Expired — drop lazily.
            self._entries.pop(key, None)
            return None
        return entry.value

    async def get_or_fetch(self, key: Any, fetch) -> T:
        """Return the cached value for ``key``, calling ``fetch()`` on miss.

        Concurrent callers for the same key all await the first fetch
        (single-flight). Fetch errors propagate to every waiter but are
        not persisted to the TTL store; the next call retries.
        """
        async with self._lock:
            cached = self._get_fresh(key)
            if cached is not None:
                return cached
            inflight = self._inflight.get(key)
            if inflight is not None:
                # A fetch is already in flight — share the existing future.
                waiter = inflight
                owner = False
            else:
                loop = asyncio.get_running_loop()
                waiter = loop.create_future()
                self._inflight[key] = waiter
                owner = True

        if owner:
            try:
                value = await fetch()
            except BaseException as exc:  # noqa: BLE001
                async with self._lock:
                    self._inflight.pop(key, None)
                if not waiter.done():
                    waiter.set_exception(exc)
                # Owner awaits the future below to consume the exception
                # (avoids "Future exception was never retrieved" warnings
                # when no other waiter is subscribed).
            else:
                async with self._lock:
                    self._entries[key] = _CacheEntry(
                        value=value, expires_at=self._clock() + self._ttl
                    )
                    self._inflight.pop(key, None)
                if not waiter.done():
                    waiter.set_result(value)

        # Both owner and waiters await the shared future. The owner has
        # already populated it; ``await`` simply re-raises the exception
        # or returns the value, and crucially marks the future as
        # consumed.
        return await waiter

    def set(self, key: Any, value: T) -> None:
        """Store a value directly under ``key`` with a fresh TTL.

        Used to populate the cache from a bulk fetch that resolved many keys
        in one request (see ``MarketData.quotes``), bypassing the per-key
        single-flight ``get_or_fetch`` path.
        """
        self._entries[key] = _CacheEntry(
            value=value, expires_at=self._clock() + self._ttl
        )

    def invalidate(self, key: Any) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


# ---------------------------------------------------------------------------
# MarketData
# ---------------------------------------------------------------------------


# Cache TTLs — values are deliberately small enough that any single test
# run can wait one out cheaply (see test_market.py).
QUOTE_TTL_SECONDS = 10.0
ANALYSIS_TTL_SECONDS = 5 * 60.0
HISTORY_TTL_SECONDS = 60 * 60.0
EARNINGS_TTL_SECONDS = 6 * 60 * 60.0  # 6 hours
IV_TTL_SECONDS = 60 * 60.0  # 1 hour, per ADR-0002
REALIZED_MOVE_TTL_SECONDS = 24 * 60 * 60.0  # 24h — computed from stable historical data
CONTEXT_TTL_SECONDS = 24 * 60 * 60.0  # 24h — profile/ratios/news change at most daily


class MarketData:
    """Single in-process seam for all FMP traffic.

    Construct once at app startup, share via `app.state.market`. All
    methods are coroutine-safe and de-dupe identical concurrent requests.
    """

    def __init__(
        self,
        client: FMPClient,
        scanner: StockScanner,
        finnhub_client=None,
        tastytrade_client=None,
        *,
        quote_ttl: float = QUOTE_TTL_SECONDS,
        analysis_ttl: float = ANALYSIS_TTL_SECONDS,
        history_ttl: float = HISTORY_TTL_SECONDS,
        earnings_ttl: float = EARNINGS_TTL_SECONDS,
        iv_ttl: float = IV_TTL_SECONDS,
        realized_move_ttl: float = REALIZED_MOVE_TTL_SECONDS,
        context_ttl: float = CONTEXT_TTL_SECONDS,
        clock=time.monotonic,
    ):
        self.client = client
        self.scanner = scanner
        self._finnhub = finnhub_client
        self._tastytrade = tastytrade_client
        self._quote_cache: _TTLSingleflightCache[PriceQuote] = _TTLSingleflightCache(
            quote_ttl, clock=clock
        )
        self._analysis_cache: _TTLSingleflightCache[TechnicalAnalysis] = (
            _TTLSingleflightCache(analysis_ttl, clock=clock)
        )
        self._history_cache: _TTLSingleflightCache[HistoryFrame] = (
            _TTLSingleflightCache(history_ttl, clock=clock)
        )
        self._earnings_cache: _TTLSingleflightCache[list] = _TTLSingleflightCache(
            earnings_ttl, clock=clock
        )
        self._iv_cache: _TTLSingleflightCache[IVSnapshotRaw] = _TTLSingleflightCache(
            iv_ttl, clock=clock
        )
        self._realized_move_cache: _TTLSingleflightCache[list] = _TTLSingleflightCache(
            realized_move_ttl, clock=clock
        )
        self._context_cache: _TTLSingleflightCache[dict] = _TTLSingleflightCache(
            context_ttl, clock=clock
        )

    # -- quotes -------------------------------------------------------------

    async def quote(self, ticker: str) -> PriceQuote:
        """Fetch one quote. Raises `MarketDataError` on failure."""
        symbol = ticker.upper()
        return await self._quote_cache.get_or_fetch(
            ("quote", symbol), lambda: self._fetch_quote(symbol)
        )

    async def _fetch_quote(self, ticker: str) -> PriceQuote:
        try:
            raw = await self.client.get_quote(ticker)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(ticker, str(exc)) from exc
        if not raw:
            raise TickerNotFound(ticker)
        return _quote_from_raw(ticker, raw)

    async def quotes(self, tickers: list[str]) -> dict[str, PriceQuote | MarketDataError]:
        """Fetch multiple quotes in ONE bulk FMP request; per-ticker errors are
        returned, not raised.

        Fresh cache hits are served without any request. Cache misses are
        fetched together via FMP's bulk endpoint (one call instead of N) and
        written back to the per-ticker cache, so subsequent single ``quote()``
        calls reuse them. A ticker absent from the bulk response surfaces as
        ``TickerNotFound``; a whole-request failure as ``UpstreamError``.
        """
        # Dedupe while preserving order.
        unique: list[str] = []
        seen: set[str] = set()
        for t in tickers:
            s = t.upper()
            if s not in seen:
                seen.add(s)
                unique.append(s)

        results: dict[str, PriceQuote | MarketDataError] = {}
        misses: list[str] = []
        for s in unique:
            cached = self._quote_cache._get_fresh(("quote", s))
            if cached is not None:
                results[s] = cached
            else:
                misses.append(s)

        if not misses:
            return results

        try:
            raw_map = await self.client.get_batch_quotes(misses)
            batch_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001 — surface as per-ticker error
            raw_map = {}
            batch_error = str(exc)

        # The FMP client swallows HTTP failures and returns an empty map, so
        # a revoked key would otherwise surface as TickerNotFound for every
        # ticker — indistinguishable from delisted symbols.
        if batch_error is None:
            batch_error = self._upstream_failure()

        for s in misses:
            raw = raw_map.get(s)
            if raw:
                try:
                    quote = _quote_from_raw(s, raw)
                except MarketDataError as exc:
                    results[s] = exc
                except Exception as exc:  # noqa: BLE001
                    results[s] = UpstreamError(s, str(exc))
                else:
                    self._quote_cache.set(("quote", s), quote)
                    results[s] = quote
            elif batch_error is not None:
                results[s] = UpstreamError(s, batch_error)
            else:
                results[s] = TickerNotFound(s)
        return results

    # -- analyses -----------------------------------------------------------

    async def analysis(self, ticker: str) -> TechnicalAnalysis:
        """Full technical analysis for one ticker."""
        symbol = ticker.upper()
        return await self._analysis_cache.get_or_fetch(
            ("analysis", symbol), lambda: self._fetch_analysis(symbol)
        )

    async def _fetch_analysis(self, ticker: str) -> TechnicalAnalysis:
        try:
            raw = await self.scanner.analyze_ticker(ticker)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(ticker, str(exc)) from exc
        if not raw:
            raise TickerNotFound(ticker)
        return _analysis_from_raw(ticker, raw)

    async def analyses(
        self, tickers: list[str]
    ) -> dict[str, TechnicalAnalysis | MarketDataError]:
        """Per-ticker analyses; errors returned, not raised."""
        symbols = [t.upper() for t in tickers]
        unique: list[str] = []
        seen: set[str] = set()
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        results: dict[str, TechnicalAnalysis | MarketDataError] = {}
        await asyncio.gather(
            *[self._gather_one(s, self.analysis, results) for s in unique],
            return_exceptions=False,
        )
        # As in ``quotes``: a failed history fetch returns None, which
        # reads as TickerNotFound. Reclassify when the provider failed.
        failure = self._upstream_failure()
        if failure is not None:
            for s, value in results.items():
                if isinstance(value, TickerNotFound):
                    results[s] = UpstreamError(s, failure)
        return results

    def _upstream_failure(self) -> Optional[str]:
        """Describe the client's last request failure if it was the
        provider's fault (bad key, 5xx, rate limit, network), else None.

        A 404 / empty answer for one symbol is not an upstream failure.
        ``last_failure`` is shared client state, so this is a heuristic for
        batch calls: in an outage every request fails the same way.
        """
        failure = getattr(self.client, "last_failure", None)
        if not isinstance(failure, RequestFailure) or failure.is_no_data():
            return None
        return failure.describe()

    # -- history ------------------------------------------------------------

    async def history(self, ticker: str, days: int = 252) -> HistoryFrame:
        symbol = ticker.upper()
        return await self._history_cache.get_or_fetch(
            ("history", symbol, int(days)),
            lambda: self._fetch_history(symbol, days),
        )

    async def _fetch_history(self, ticker: str, days: int) -> HistoryFrame:
        try:
            raw = await self.client.get_historical_data(ticker, days=days)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(ticker, str(exc)) from exc
        if not raw:
            raise TickerNotFound(ticker)
        bars = tuple(HistoryBar(**_normalise_bar(b)) for b in raw)
        return HistoryFrame(ticker=ticker, bars=bars)

    # -- earnings calendar --------------------------------------------------

    async def earnings_calendar(self, start: date, end: date) -> list[dict]:
        """Fetch upcoming earnings events between ``start`` and ``end``.

        Results are cached for 6 hours per (start, end) key. Concurrent
        callers for the same key share one in-flight request (single-flight).
        Maps Finnhub failures to :class:`EarningsDateUnknown`.
        """
        key = ("earnings_calendar", start.isoformat(), end.isoformat())
        return await self._earnings_cache.get_or_fetch(
            key, lambda: self._fetch_earnings_calendar(start, end)
        )

    async def _fetch_earnings_calendar(self, start: date, end: date) -> list[dict]:
        if self._finnhub is None:
            raise EarningsDateUnknown(
                "FinnhubClient not configured — set FINNHUB_API_KEY"
            )
        try:
            events = await self._finnhub.get_earnings_calendar(start, end)
        except Exception as exc:  # noqa: BLE001
            raise EarningsDateUnknown(str(exc)) from exc
        return events

    # -- iv snapshots -------------------------------------------------------

    async def iv_snapshot(self, ticker: str) -> IVSnapshotRaw:
        """Fetch today's IV snapshot for ``ticker``.

        Raises :class:`OptionsDataUnavailable` if no client is configured
        or the client cannot price a usable snapshot. 1 hour TTL +
        single-flight per ADR-0002.
        """
        symbol = ticker.upper()
        return await self._iv_cache.get_or_fetch(
            ("iv_snapshot", symbol), lambda: self._fetch_iv_snapshot(symbol)
        )

    async def _fetch_iv_snapshot(self, ticker: str) -> IVSnapshotRaw:
        if self._tastytrade is None:
            raise OptionsDataUnavailable(
                ticker,
                "TastytradeClient not configured — set TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN",
                reason="not_configured",
            )
        try:
            return await self._tastytrade.get_iv_snapshot(ticker)
        except MarketDataError:
            raise
        except TastytradeStaleQuote as exc:
            raise OptionsDataUnavailable(ticker, str(exc), reason="stale_quote") from exc
        except TastytradeRateLimited as exc:
            raise OptionsDataUnavailable(ticker, str(exc), reason="rate_limited") from exc
        except TastytradeUnavailable as exc:
            raise OptionsDataUnavailable(ticker, str(exc), reason="no_data") from exc
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataUnavailable(ticker, str(exc), reason="error") from exc

    async def iv_snapshots(
        self, tickers: list[str]
    ) -> dict[str, IVSnapshotRaw | MarketDataError]:
        """Per-ticker IV snapshots; errors returned, not raised."""
        symbols = [t.upper() for t in tickers]
        unique: list[str] = []
        seen: set[str] = set()
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        results: dict[str, IVSnapshotRaw | MarketDataError] = {}
        await asyncio.gather(
            *[self._gather_one(s, self.iv_snapshot, results) for s in unique],
            return_exceptions=False,
        )
        return results

    # -- realized move history ----------------------------------------------

    async def realized_move_history(
        self,
        ticker: str,
        events: list[tuple[date, str]],
        quarters: int = 8,
    ) -> list[Optional[Decimal]]:
        """Compute close-to-close realized moves for ``events`` per ADR-0003.

        ``events`` is a list of ``(report_date, report_time)`` pairs sourced
        from ``earnings_events`` — callers pull them from the DB. FMP price
        history is fetched once and cached with a 24h TTL + single-flight.

        Returns one ``Decimal | None`` per event in the same order. ``None``
        means the surrounding price bars were not available (ticker too new,
        event too old, or FMP returned nothing).
        """
        symbol = ticker.upper()
        # Cache key includes the sorted events tuple for stability.
        sorted_events = tuple(sorted(events, reverse=True))[:quarters]
        key = ("realized_move_history", symbol, sorted_events)
        return await self._realized_move_cache.get_or_fetch(
            key,
            lambda: self._compute_realized_move_history(symbol, sorted_events),
        )

    async def _compute_realized_move_history(
        self,
        ticker: str,
        events: tuple[tuple[date, str], ...],
    ) -> list[Optional[Decimal]]:
        try:
            frame = await self.history(ticker, days=1500)
        except MarketDataError:
            return [None] * len(events)
        bars = [{"date": str(b.date), "close": float(b.close)} for b in frame.bars]
        return [
            compute_realized_move(bars, report_date, report_time)
            for report_date, report_time in events
        ]

    # -- ticker context (fundamentals + news) -------------------------------

    async def ticker_context(self, ticker: str) -> dict:
        """Fundamental context block for the ticker detail page.

        Fetches company profile, key metrics TTM, quarterly income (4Q), and
        recent news in parallel. Results are cached with a 24h TTL so the
        page loads fast on repeated visits and FMP call cost stays minimal.

        Fetch on click only — never called at scan time (scanner cost unchanged).
        """
        symbol = ticker.upper()
        return await self._context_cache.get_or_fetch(
            ("context", symbol),
            lambda: self._fetch_ticker_context(symbol),
        )

    async def _fetch_ticker_context(self, ticker: str) -> dict:
        results = await asyncio.gather(
            self.client.get_company_profile(ticker),
            self.client.get_ratios_ttm(ticker),
            self.client.get_income_statements(ticker, limit=4),
            self.client.get_stock_news(ticker, limit=5),
            self.client.get_earnings_date(ticker),
            return_exceptions=True,
        )
        profile_raw, ratios_raw, income_raw, news_raw, earnings_raw = results

        # --- profile ---------------------------------------------------
        # The client methods already unwrap FMP's single-element list and
        # return a dict (or None / an exception via gather).
        if isinstance(profile_raw, Exception):
            # The profile request itself failed (bad key, 5xx, network) —
            # an outage, not an unknown symbol. Surfacing it as
            # TickerNotFound made an FMP outage look like bad input.
            raise UpstreamError(ticker, str(profile_raw)) from profile_raw
        profile: dict = profile_raw if isinstance(profile_raw, dict) else {}
        if not profile:
            # No profile = FMP doesn't know the symbol. Raise instead of
            # caching an all-None context so the route's 404 actually fires.
            raise TickerNotFound(f"No profile data for {ticker}")

        company_name = profile.get("companyName") or None
        sector = profile.get("sector") or None
        description = profile.get("description") or None

        current_price: Optional[float] = None
        high_52w: Optional[float] = None
        raw_price = profile.get("price")
        if raw_price is not None:
            try:
                current_price = float(raw_price) or None
            except (ValueError, TypeError):
                pass
        range_str = profile.get("range", "") or ""
        if "-" in range_str:
            parts = range_str.split("-")
            if len(parts) >= 2:
                try:
                    high_52w = float(parts[-1])
                except ValueError:
                    pass

        drawdown_from_52w_high_pct: Optional[float] = None
        if current_price and high_52w and high_52w > 0:
            drawdown_from_52w_high_pct = round(
                (high_52w - current_price) / high_52w * 100, 1
            )

        # --- TTM ratios --------------------------------------------------
        ratios: dict = ratios_raw if isinstance(ratios_raw, dict) else {}

        net_margin_ttm: Optional[float] = None
        debt_to_equity: Optional[float] = None
        nm = ratios.get("netProfitMarginTTM")
        de = ratios.get("debtToEquityRatioTTM")
        if nm is not None:
            try:
                net_margin_ttm = round(float(nm) * 100, 1)
            except (ValueError, TypeError):
                pass
        if de is not None:
            try:
                debt_to_equity = round(float(de), 2)
            except (ValueError, TypeError):
                pass

        # --- income statements (most recent first → reverse for display) ---
        quarterly_financials: list[dict] = []
        income_stmts: list[dict] = income_raw if isinstance(income_raw, list) else []
        chron = list(reversed(income_stmts[:4]))
        prev_revenue: Optional[int] = None
        prev_eps: Optional[float] = None
        for stmt in chron:
            raw_rev = stmt.get("revenue")
            raw_eps = stmt.get("eps")
            revenue: Optional[int] = int(float(raw_rev)) if raw_rev else None
            eps: Optional[float] = float(raw_eps) if raw_eps else None
            # /stable/income-statement has no netIncomeRatio — compute it.
            raw_ni = stmt.get("netIncome")
            net_margin: Optional[float] = (
                round(float(raw_ni) / float(raw_rev) * 100, 1)
                if raw_ni is not None and raw_rev
                else None
            )
            rev_change: Optional[float] = None
            if revenue and prev_revenue:
                rev_change = round((revenue - prev_revenue) / abs(prev_revenue) * 100, 1)
            eps_change: Optional[float] = None
            if eps is not None and prev_eps is not None and prev_eps != 0.0:
                eps_change = round((eps - prev_eps) / abs(prev_eps) * 100, 1)
            quarterly_financials.append({
                "quarter": _quarter_label(stmt.get("date", "")),
                "revenue": revenue,
                "revenue_pct_change": rev_change,
                "eps": eps,
                "eps_pct_change": eps_change,
                "net_margin": net_margin,
            })
            prev_revenue = revenue
            prev_eps = eps

        # --- news ----------------------------------------------------------
        headlines: list[dict] = []
        news_list: list[dict] = news_raw if isinstance(news_raw, list) else []
        for article in news_list[:5]:
            if not isinstance(article, dict):
                continue
            headlines.append({
                "title": article.get("title") or "",
                "url": article.get("url") or "",
                "published_at": article.get("publishedDate") or "",
                "source": article.get("site") or "",
            })

        # --- next earnings -------------------------------------------------
        next_earnings_date: Optional[str] = None
        days_to_earnings: Optional[int] = None
        if isinstance(earnings_raw, dict):
            next_earnings_date = earnings_raw.get("date")
            days_to_earnings = earnings_raw.get("days_until")

        return {
            "ticker": ticker,
            "company_name": company_name,
            "sector": sector,
            "description": description,
            "current_price": current_price,
            "high_52w": high_52w,
            "drawdown_from_52w_high_pct": drawdown_from_52w_high_pct,
            "quarterly_financials": quarterly_financials,
            "net_margin_ttm": net_margin_ttm,
            "debt_to_equity": debt_to_equity,
            "next_earnings_date": next_earnings_date,
            "days_to_earnings": days_to_earnings,
            "headlines": headlines,
        }

    # -- helpers ------------------------------------------------------------

    async def _gather_one(
        self,
        ticker: str,
        fetcher,
        sink: dict[str, Any],
    ) -> None:
        try:
            sink[ticker] = await fetcher(ticker)
        except MarketDataError as exc:
            sink[ticker] = exc
        except Exception as exc:  # noqa: BLE001 — last-ditch safety net
            sink[ticker] = UpstreamError(ticker, str(exc))


# ---------------------------------------------------------------------------
# ADR-0003 realized-move formula
# ---------------------------------------------------------------------------


def compute_realized_move(
    bars: list[dict],
    report_date: date,
    report_time: str,
) -> Optional[Decimal]:
    """Return abs((post_close - pre_close) / pre_close) per ADR-0003.

    ``bars`` is a list of ``{"date": "YYYY-MM-DD", "close": float}`` sorted
    in any order (oldest or newest first) — only calendar ordering matters.

    ``report_time``:
    - ``"bmo"``  → post_close = first bar on or after ``report_date``
    - ``"amc"``  → post_close = first bar strictly after ``report_date``
    - anything else ("unknown") → same as ``"amc"``

    Returns ``None`` when either price anchor is unavailable.
    """
    date_to_close: dict[date, float] = {}
    for bar in bars:
        try:
            d = date.fromisoformat(str(bar["date"]))
            date_to_close[d] = float(bar["close"])
        except (KeyError, ValueError, TypeError):
            continue

    if not date_to_close:
        return None

    sorted_dates = sorted(date_to_close)

    # pre_close: last regular session strictly before report_date.
    pre_candidates = [d for d in sorted_dates if d < report_date]
    if not pre_candidates:
        return None
    pre_close = date_to_close[pre_candidates[-1]]

    # post_close: same day for bmo (first bar >= report_date),
    # next session for amc/unknown (first bar > report_date).
    if report_time == "bmo":
        post_candidates = [d for d in sorted_dates if d >= report_date]
    else:
        post_candidates = [d for d in sorted_dates if d > report_date]

    if not post_candidates:
        return None
    post_close = date_to_close[post_candidates[0]]

    if pre_close == 0.0:
        return None

    return abs(Decimal(str((post_close - pre_close) / pre_close)))


# ---------------------------------------------------------------------------
# Conversions from raw FMP / scanner dicts to frozen schemas
# ---------------------------------------------------------------------------


def _quote_from_raw(ticker: str, raw: dict) -> PriceQuote:
    # A quote without a usable price is no quote. Coercing a null/missing
    # price to 0.0 made every "price < X" alert fire, so surface it the same
    # way as a ticker absent from the response.
    raw_price = raw.get("price")
    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is None or not price > 0:
        raise TickerNotFound(ticker, f"no usable price in quote ({raw_price!r})")
    change = float(raw.get("change") or 0.0)
    change_pct = float(
        raw.get("changesPercentage", raw.get("changePercentage", 0.0)) or 0.0
    )
    return PriceQuote(
        ticker=ticker,
        price=price,
        change=change,
        change_pct=change_pct,
        volume=int(raw.get("volume") or 0),
        avg_volume=int(raw.get("avgVolume", raw.get("averageVolume", 0)) or 0),
        market_cap=int(raw.get("marketCap") or 0),
    )


_ANALYSIS_FIELDS = {
    "ticker",
    "price",
    "market_cap",
    "volume",
    "avg_volume",
    "volume_pace",
    "volume_pace_reliable",
    "hv_rank",
    "current_hv",
    "trend",
    "dist_50",
    "dist_200",
    "ma_slope",
    "range_position",
    "high_52w",
    "low_52w",
    "support",
    "resistance",
    "support_type",
    "resistance_type",
    "volume_ratio",
    "rsi",
    "momentum_10d",
    "setup",
    "days_since_low",
    "pct_above_low",
    "rsi_floor_delta",
    "undercut_decelerating",
    "post_low_trend",
    "retests_held",
    "hv_vs_peak",
    "reclaim_streak",
    "earnings_date",
    "days_to_earnings",
    "score",
    "signals",
    "change",
    "change_pct",
}


def _analysis_from_raw(ticker: str, raw: dict) -> TechnicalAnalysis:
    payload: dict[str, Any] = {"ticker": ticker}
    for key, value in raw.items():
        if key == "signals" and isinstance(value, list):
            payload["signals"] = tuple(value)
        elif key in _ANALYSIS_FIELDS:
            payload[key] = value
    if "price" not in payload:
        payload["price"] = float(raw.get("price") or 0.0)
    return TechnicalAnalysis(**payload)


def _normalise_bar(raw: dict) -> dict[str, Any]:
    # FMP returns "date" as a string key already; defensive cast on numerics.
    return {
        "date": str(raw.get("date") or ""),
        "open": float(raw.get("open") or 0.0),
        "high": float(raw.get("high") or 0.0),
        "low": float(raw.get("low") or 0.0),
        "close": float(raw.get("close") or 0.0),
        "volume": int(raw.get("volume") or 0),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quarter_label(date_str: str) -> str:
    """Convert 'YYYY-MM-DD' to 'Q1 YYYY' etc."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        quarter = (dt.month - 1) // 3 + 1
        return f"Q{quarter} {dt.year}"
    except (ValueError, TypeError):
        return date_str or ""


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def get_market(request: Request) -> MarketData:
    """Resolve the singleton `MarketData` from app state.

    Tests override this via `app.dependency_overrides[get_market]`. Issue
    #3 will move all such providers into a dedicated `dependencies.py`
    module; for now we co-locate with the service to avoid touching that
    file.
    """
    market = getattr(request.app.state, "market", None)
    if market is None:
        raise RuntimeError(
            "MarketData not initialised — check FastAPI lifespan startup."
        )
    return market
