"""
Stock Scanner Engine - Async version
Main scanning logic with parallel processing
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import uuid4

import pandas as pd
import pytz

from app.services.fmp_client import FMPClient, prune_dated_cache_files
from app.services.funnel import dollar_volume
from app.services.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)

# Stage-1 cache files roll over on the US-equity trading date, not the
# container's clock (UTC on Cloud Run would roll at 8pm ET). pytz rather
# than zoneinfo: the slim runtime image may not ship system tzdata.
_ET = pytz.timezone("America/New_York")


# Benchmark for "last earnings reaction vs SPY" (#117). One cached
# historical fetch per day, shared by every row.
BENCHMARK = "SPY"


def _closes_by_date(bars) -> dict[str, float]:
    """``{"YYYY-MM-DD": close}`` from FMP bars; unparseable bars dropped."""
    out: dict[str, float] = {}
    for bar in bars or []:
        try:
            out[str(bar["date"])[:10]] = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def earnings_reaction(
    closes: dict[str, float],
    benchmark: dict[str, float],
    report_date: Optional[str],
) -> tuple[Optional[float], Optional[float]]:
    """Signed % move around a report, raw and minus SPY's move (#117).

    Window: last close strictly before the report date -> close of the
    first session strictly after it. FMP's earnings feed carries no
    before-open / after-close flag, so one window covers both: exact for
    after-close reporters, one extra session of drift for before-open
    ones. None until that next session has closed. The SPY move uses the
    same two dates; if SPY lacks either, only the raw move is returned.
    """
    if not report_date or not closes:
        return None, None
    dates = sorted(closes)
    before = [d for d in dates if d < report_date]
    after = [d for d in dates if d > report_date]
    if not before or not after:
        return None, None
    d0, d1 = before[-1], after[0]
    if closes[d0] <= 0:
        return None, None
    move = (closes[d1] / closes[d0] - 1) * 100
    b0, b1 = benchmark.get(d0), benchmark.get(d1)
    if not b0 or not b1 or b0 <= 0:
        return round(move, 2), None
    return round(move, 2), round(move - (b1 / b0 - 1) * 100, 2)


def eps_surprise_pct(actual: Optional[float], estimate: Optional[float]) -> Optional[float]:
    """(actual - estimate) / |estimate| in %. None without both, or when the
    estimate is zero (no meaningful percentage)."""
    if actual is None or estimate is None or estimate == 0:
        return None
    return round((actual - estimate) / abs(estimate) * 100, 1)


def universe_digest(tickers) -> str:
    """Short fingerprint of a ticker list. Stored with the Stage-1 cache so
    a survivor list is only reused for the exact universe it was cut from:
    ``combined`` is a screener build since #118 and can change between
    scans on the same day (weekly rebuild, or the static fallback)."""
    return hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:12]


def _passes_stage1(quote: dict) -> bool:
    """Stage-1 filter: quality names only (Daneshire thesis: higher-priced stocks)."""
    price = quote.get("price", 0) or 0
    volume = quote.get("volume", 0) or 0
    market_cap = quote.get("marketCap", 0) or 0
    return price >= 20 and volume > 10000 and market_cap > 1_000_000_000


class StockScanner:
    """
    Stock scanner for swing trading and options strategies.

    Features:
    - Two-stage scanning (fast filter -> deep analysis)
    - Daily caching to reduce API calls
    - Async parallel processing
    - Comprehensive technical analysis
    - Opportunity scoring
    """

    def __init__(
        self,
        api_key: str = None,
        cache_dir: str = "scanner_cache",
        client: Optional[FMPClient] = None,
    ):
        # Accept an injected ``FMPClient`` so the throttle + retry budget can
        # be shared across the whole process (see
        # ``app.services.market.MarketData``). The default preserves the
        # pre-existing behaviour for any direct caller.
        self.client = client if client is not None else FMPClient(api_key=api_key)
        self.cache_dir = cache_dir
        self.indicators = TechnicalIndicators()
        # (ET date, SPY closes by date) — see ``_load_benchmark``.
        self._benchmark: tuple[Optional[str], dict[str, float]] = (None, {})

        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        prune_dated_cache_files(cache_dir)

    def _get_cache_path(self, cache_name: str) -> str:
        """Get path for today's cache file."""
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        return os.path.join(self.cache_dir, f"{cache_name}_{today}.json")

    def _load_cache(self, cache_name: str) -> Optional[dict]:
        """Load cached data if it exists for today."""
        cache_path = self._get_cache_path(cache_name)
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                return json.load(f)
        return None

    def _save_cache(self, cache_name: str, data: dict):
        """Save data to cache."""
        cache_path = self._get_cache_path(cache_name)
        with open(cache_path, "w") as f:
            json.dump(data, f)

    @staticmethod
    def _cached_survivor_tickers(cache_data: dict) -> list[str]:
        """Survivor tickers from a Stage-1 cache file.

        Current format is ``{"tickers": [...]}``. Files written before the
        fresh-quote fix mapped ticker -> quote; only their keys are used, so
        a same-day legacy file never feeds stale quotes into Stage 2.
        """
        tickers = cache_data.get("tickers")
        if isinstance(tickers, list):
            return [t for t in tickers if isinstance(t, str)]
        return list(cache_data.keys())

    async def _load_benchmark(self) -> dict[str, float]:
        """SPY closes for the earnings-reaction column, fetched once per ET
        day (the FMP client caches the bars on disk too). Best effort: on
        any failure the vs-SPY column is just empty."""
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        if self._benchmark[0] != today:
            try:
                bars = await self.client.get_historical_data(BENCHMARK, days=252)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Benchmark {BENCHMARK} bars unavailable: {e}")
                bars = None
            closes = _closes_by_date(bars)
            # Only memoize a real series, so a blip retries next scan.
            self._benchmark = (today if closes else None, closes)
        return self._benchmark[1]

    async def _process_ticker(self, ticker: str, quote: dict) -> Optional[dict]:
        """
        Process a single ticker with full technical analysis.
        Returns comprehensive metrics dict or None if insufficient data.
        """
        try:
            # Read-only on the history cache: one scan touches every
            # survivor once, and keeping each full series on Cloud Run's
            # in-memory disk would outgrow the instance (#118).
            hist_data = await self.client.get_historical_data(
                ticker, days=252, save_cache=False
            )

            if hist_data is None or len(hist_data) < 20:
                return None

            result = self._analyze_frame(ticker, quote, pd.DataFrame(hist_data))
            if result is None:
                return None

            # Next and last report: one /stable/earnings call (#117 adds the
            # last-report facts from the same response).
            earnings = await self.client.get_earnings_info(ticker) or {}
            result["earnings_date"] = earnings.get("next_date")
            result["days_to_earnings"] = earnings.get("days_until")
            last_date = earnings.get("last_date")
            actual = earnings.get("last_eps_actual")
            estimate = earnings.get("last_eps_estimate")
            reaction, reaction_vs_spy = earnings_reaction(
                _closes_by_date(hist_data), self._benchmark[1], last_date
            )
            result["last_earnings_date"] = last_date
            result["last_eps_actual"] = actual
            result["last_eps_estimate"] = estimate
            result["eps_surprise_pct"] = eps_surprise_pct(actual, estimate)
            result["earnings_reaction_pct"] = reaction
            result["earnings_reaction_vs_spy"] = reaction_vs_spy

            return result

        except Exception as e:
            logger.debug(f"Error processing {ticker}: {e}")
            return None

    def _analyze_frame(self, ticker: str, quote: dict, df: pd.DataFrame) -> Optional[dict]:
        """
        Compute all indicators for one ticker from an OHLCV frame (oldest to
        newest). Split from ``_process_ticker`` so tests can drive the full
        analysis + scoring path from fixture bars without any network.
        """
        current_price = quote.get("price", 0)
        current_volume = quote.get("volume", 0)

        if current_price <= 0:
            return None

        # Calculate all indicators
        avg_volume = self.indicators.calculate_avg_volume(df, days=20)

        if avg_volume and avg_volume > 0:
            volume_pace, is_market_hours = self.indicators.calculate_volume_pace(
                current_volume, avg_volume
            )
        else:
            volume_pace, is_market_hours = 1.0, False

        vol_data = self.indicators.calculate_volatility_rank(df)
        trend = self.indicators.calculate_trend(current_price, df)
        range_52w = self.indicators.calculate_52w_position(current_price, df)
        levels = self.indicators.calculate_support_resistance(current_price, df)
        momentum = self.indicators.calculate_momentum(
            current_price, current_volume, df
        )
        recovery = self.indicators.calculate_recovery_metrics(current_price, df)

        result = {
            "ticker": ticker,
            "price": current_price,
            "market_cap": int(quote.get("marketCap", 0) or 0),
            "volume": int(current_volume or 0),
            "avg_volume": int(avg_volume or 0),
            "dollar_volume": (
                dollar_volume(df["close"], df["volume"])
                if {"close", "volume"} <= set(df.columns)
                else None
            ),
            "volume_pace": volume_pace,
            "volume_pace_reliable": is_market_hours,
            "hv_rank": vol_data["rank"] if vol_data else None,
            "current_hv": vol_data["current_hv"] if vol_data else None,
            "trend": trend["trend"] if trend else "Unknown",
            "dist_50": trend["dist_50"] if trend else None,
            "dist_200": trend["dist_200"] if trend else None,
            "ma_slope": trend.get("ma_slope") if trend else None,
            # Fact column (#117): % below the 52-week high, <= 0. A live
            # price above the newest bar's high counts as at the high.
            "pct_off_high": (
                round(min(0.0, (current_price / range_52w["high_52w"] - 1) * 100), 2)
                if range_52w["high_52w"]
                else None
            ),
            "range_position": range_52w["position_pct"],
            "high_52w": range_52w["high_52w"],
            "low_52w": range_52w["low_52w"],
            "support": levels["support"],
            "resistance": levels["resistance"],
            "support_type": levels.get("support_type", "unknown"),
            "resistance_type": levels.get("resistance_type", "unknown"),
            "volume_ratio": momentum["volume_ratio"],
            "rsi": momentum["rsi"],
            "momentum_10d": momentum["momentum_10d"],
        }

        if recovery:
            result.update(recovery)

        return result

    @staticmethod
    def _score_opportunity(result: dict) -> tuple[int, list[str], Optional[str]]:
        """
        LEGACY SCORE (#117) — no longer the scanner's default sort. Kept
        visible as a "Legacy score" column for one quarter so its forward
        record stays comparable; remove around 2027-01-10. It was fit to five
        hand-picked trades and its top 15 trailed SPY out-of-sample (ADR-0007
        verdict, #114). Do not tune it.

        Score a stock opportunity. Two tracks (issue #49):

        - Recovery: >= 20% drawdown from the 52w high qualifies a stock for
          this track but earns 0 points itself. Points measure whether the
          falling has STOPPED — RSI floor direction across successive swing
          lows (heaviest, signed), undercut deceleration, and the post-low
          bounce holding vs fading — plus confirmations (HV decay, retests
          held, 20-MA reclaim) that never gate.
        - Uptrend pullback: the pre-existing pullback-to-rising-50MA logic,
          unchanged.

        Volume pace and earnings proximity are informational only.
        Returns (score, signal descriptions, setup label or None).
        """
        score = 0
        signals = []
        setup = None

        price = result.get("price") or 0
        high_52w = result.get("high_52w") or 0
        drawdown = ((price - high_52w) / high_52w * 100) if high_52w > 0 else 0.0

        rsi_floor_delta = result.get("rsi_floor_delta")
        days_since_low = result.get("days_since_low")

        if drawdown <= -20 and days_since_low is not None:
            # ---- Recovery track ----
            signals.append(f"Recovery candidate ({drawdown:.0f}% off 52w high)")

            # Core: force of selling at the newest swing low vs the prior one
            if rsi_floor_delta is not None:
                if rsi_floor_delta >= 5:
                    score += 30
                    signals.append(f"RSI floor rising strongly (+{rsi_floor_delta:.0f})")
                elif rsi_floor_delta > 0:
                    score += 20
                    signals.append(f"RSI floor rising (+{rsi_floor_delta:.0f})")
                elif rsi_floor_delta <= -5:
                    score -= 30
                    signals.append(f"RSI floor falling ({rsi_floor_delta:.0f})")
                else:
                    score -= 20
                    signals.append(f"RSI floor slipping ({rsi_floor_delta:.0f})")

            # Core: undercut trajectory. Acceleration is only bearish when the
            # RSI floor is ALSO falling — a big final undercut on rising RSI
            # is the capitulation-flush shape of a just-bottomed turn (ESTC).
            undercut_decel = result.get("undercut_decelerating")
            if undercut_decel is True:
                score += 15
                signals.append("Undercuts shrinking")
            elif (
                undercut_decel is False
                and rsi_floor_delta is not None
                and rsi_floor_delta < 0
            ):
                score -= 15
                signals.append("Undercuts deepening")

            # Core: is the bounce off the low holding or fading back into it?
            post_low_trend = result.get("post_low_trend")
            if post_low_trend == "rising":
                score += 15
                signals.append("Higher 10d lows since bottom")
            elif post_low_trend == "fading":
                score -= 15
                signals.append("Bounce fading back toward low")

            # Confirmations
            hv_vs_peak = result.get("hv_vs_peak")
            if hv_vs_peak is not None and hv_vs_peak <= 60:
                score += 10
                signals.append(f"Volatility draining ({hv_vs_peak:.0f}% of 90d peak)")

            retests = result.get("retests_held") or 0
            if retests >= 1:
                score += 10
                plural = "s" if retests > 1 else ""
                signals.append(f"Held {retests} retest{plural} of the low")

            reclaim = result.get("reclaim_streak") or 0
            if reclaim >= 3:
                score += 10
                signals.append(f"{reclaim} closes above 20-MA")

            # Label from core character, before the liquidity bonus — a mega
            # cap knife is still a knife.
            if post_low_trend == "fading":
                setup = "Fading bounce"
            elif score < 0:
                setup = "Knife"
            elif days_since_low <= 15:
                setup = "Just bottomed"
            else:
                setup = "Basing"
        else:
            # ---- Uptrend-pullback track ----
            dist_50 = result.get("dist_50")
            if result["trend"] == "Pullback in Uptrend":
                setup = "Uptrend pullback"
                if dist_50 is not None:
                    if -2 < dist_50 <= 0:
                        score += 30
                        signals.append("At 50-MA support (prime entry)")
                    elif -5 < dist_50 < -2:
                        score += 20
                        signals.append("Near 50-MA support")
            elif result["trend"] == "Uptrend":
                if dist_50 is not None and 0 < dist_50 < 5:
                    score += 15
                    signals.append("Uptrend, near MA")

        # Liquidity bonus
        market_cap = result.get("market_cap", 0)
        if market_cap > 50_000_000_000:
            score += 10
            signals.append("Mega cap (liquid)")
        elif market_cap > 10_000_000_000:
            score += 5
            signals.append("Large cap")

        # Earnings proximity warning (informational)
        days_to_earnings = result.get("days_to_earnings")
        if days_to_earnings is not None:
            earnings_date = result.get("earnings_date", "")
            if days_to_earnings <= 7:
                signals.append(f"Earnings in {days_to_earnings}d ({earnings_date}) - HIGH IV")
            elif days_to_earnings <= 14:
                signals.append(f"Earnings in {days_to_earnings}d ({earnings_date})")
            elif days_to_earnings <= 30:
                signals.append(f"Earnings {earnings_date}")

        return score, signals, setup

    async def run_scan(
        self,
        tickers: list[str],
        progress_callback: Optional[Callable[[dict], Any]] = None,
        use_cache: bool = True,
        universe: str = "custom",
        results_out: Optional[list] = None,
        quality_gate=None,
    ) -> tuple[str, list[dict]]:
        """
        Run a full scan on the given ticker list.

        Stage 1: Fast filter (cached survivor list, always-fresh quotes)
        Stage 2: Deep analysis with historical data — each ticker is scored
                 the moment its analysis completes (scoring is per-ticker),
                 so partial results are readable mid-scan (issue #53)
        Stage 3: Rank results

        Args:
            tickers: List of ticker symbols to scan
            progress_callback: Optional async callback for progress updates
            use_cache: Whether to reuse today's cached Stage 1 survivor
                list (their quotes are always refetched)
            universe: Universe name for cache key
            results_out: Optional caller-owned list; scored results are
                appended as they land so the caller can serve partials
                while the scan runs
            quality_gate: Optional ``ScanQualityGate`` (#116). Each row
                gets ``quality_pass`` / ``quality_fails``; without a gate
                they stay None / []

        Returns:
            tuple: (scan_id, list of analyzed stocks sorted by score)
        """
        scan_id = str(uuid4())
        cache_key = f"stage1_{universe}"

        # The instance can stay warm across days; drop yesterday's stage1 /
        # history files before writing today's (in-memory FS on Cloud Run).
        prune_dated_cache_files(self.cache_dir)
        client_cache_dir = getattr(self.client, "cache_dir", None)
        if client_cache_dir and client_cache_dir != self.cache_dir:
            prune_dated_cache_files(client_cache_dir)

        # Stage 1: Fast filter
        #
        # The cache holds only the *list* of Stage-1 survivors, never their
        # quotes. Quote price/volume are intraday values: reusing a 9:45am
        # quote at 3pm made Stage 2 score stale prices and divide morning
        # volume by the 3pm elapsed-session fraction (understated volume
        # pace, still flagged reliable). On a cache hit we refetch fresh
        # quotes for just the survivors -- one bulk call over the survivor
        # set instead of the whole universe.
        survivors: dict[str, dict] = {}
        digest = universe_digest(tickers)

        if use_cache:
            cache_data = self._load_cache(cache_key)
            if cache_data and cache_data.get("universe_digest") != digest:
                # Cut from a different ticker list under the same name (or
                # written before the digest existed): don't reuse it.
                logger.info(
                    f"Stage 1 cache for {universe} is from a different ticker "
                    "list; refetching the whole universe"
                )
                cache_data = None
            if cache_data:
                cached_tickers = self._cached_survivor_tickers(cache_data)
                if cached_tickers:
                    fresh = await self.client.get_batch_quotes(cached_tickers)
                    survivors = {t: q for t, q in fresh.items() if _passes_stage1(q)}
                    logger.info(
                        f"Loaded {len(cached_tickers)} Stage 1 survivors from "
                        f"{universe} cache; refetched {len(fresh)} fresh quotes, "
                        f"{len(survivors)} still pass"
                    )

        if not survivors:
            logger.info(f"Fetching quotes for {len(tickers)} stocks...")
            all_quotes = await self.client.get_batch_quotes(tickers)
            logger.info(f"Got {len(all_quotes)} quotes")

            # Apply Stage 1 filters
            for ticker, quote in all_quotes.items():
                if _passes_stage1(quote):
                    survivors[ticker] = quote

            logger.info(f"{len(survivors)} stocks passed Stage 1 filters")

            # Only cache Stage 1 when the quote fetch was near-complete.
            # A partial fetch (rate limiting, network blips) cached here
            # would pin every later scan to the crippled set for the rest
            # of the day.
            coverage = len(all_quotes) / len(tickers) if tickers else 0.0
            if coverage >= 0.9:
                self._save_cache(
                    cache_key, {"tickers": sorted(survivors), "universe_digest": digest}
                )
            else:
                logger.warning(
                    f"Quote coverage only {coverage:.0%} of universe; "
                    "skipping Stage 1 cache so the next scan refetches"
                )

        if not survivors:
            logger.error("No stocks passed Stage 1 filters")
            return scan_id, []

        if quality_gate is not None:
            await quality_gate.preload(survivors)

        await self._load_benchmark()

        # Stage 2: Deep analysis (parallel with semaphore)
        # Use lower concurrency to avoid rate limits
        results = results_out if results_out is not None else []
        total = len(survivors)
        completed = 0
        errors = 0
        semaphore = asyncio.Semaphore(3)  # Reduced from 5 to avoid rate limits

        logger.info(f"Starting Stage 2 analysis of {total} stocks...")

        async def process_one(ticker: str, quote: dict):
            nonlocal completed, errors
            async with semaphore:
                try:
                    result = await self._process_ticker(ticker, quote)
                    if result:
                        # Score immediately — per-ticker, no cross-ticker
                        # deps — so appended results are complete rows and
                        # partials can be served mid-scan.
                        score, signals, setup = self._score_opportunity(result)
                        result["score"] = score
                        result["signals"] = signals
                        result["setup"] = setup
                        if quality_gate is not None:
                            passed, fails = await quality_gate.evaluate(result)
                            result["quality_pass"] = passed
                            result["quality_fails"] = fails
                            result["sector"] = quality_gate.sector(ticker)
                        results.append(result)
                    else:
                        errors += 1  # Track when we can't process a ticker
                except Exception as e:
                    errors += 1
                    logger.debug(f"Exception processing {ticker}: {e}")

                completed += 1

                if progress_callback:
                    await progress_callback(
                        {
                            "type": "progress",
                            "scan_id": scan_id,
                            "current": completed,
                            "total": total,
                            "found": len(results),
                            "errors": errors,
                        }
                    )

                if completed % 20 == 0:
                    logger.info(
                        f"Processed {completed}/{total} - {len(results)} valid, {errors} errors"
                    )

        await asyncio.gather(
            *[process_one(ticker, quote) for ticker, quote in survivors.items()],
            return_exceptions=True,
        )

        logger.info(f"Stage 2 complete - {len(results)} stocks analyzed, {errors} errors")
        if quality_gate is not None:
            passed = sum(1 for r in results if r.get("quality_pass"))
            logger.info(
                f"Quality gate: {passed}/{len(results)} passed; "
                f"{quality_gate.refreshed} snapshots refreshed, "
                f"{quality_gate.refresh_failed} refresh failures"
            )

        # Warn if high error rate
        if errors > 0 and total > 0:
            error_rate = errors / total
            if error_rate > 0.5:
                logger.warning(f"High error rate: {error_rate:.0%} of stocks failed analysis (likely rate limiting)")

        if not results:
            logger.error("No stocks successfully analyzed in Stage 2")
            return scan_id, []

        # Stage 3: order by the legacy score (scoring already happened
        # per-ticker in Stage 2). The page sorts by fact columns (#117);
        # this order only fixes scan_picks ranks, so they stay comparable
        # with pre-#117 picks.
        sorted_results = sorted(results, key=lambda x: x["score"], reverse=True)

        if progress_callback:
            await progress_callback(
                {
                    "type": "complete",
                    "scan_id": scan_id,
                    "total_analyzed": len(sorted_results),
                }
            )

        return scan_id, sorted_results

    async def analyze_ticker(self, ticker: str) -> Optional[dict]:
        """
        Analyze a single ticker with full technical breakdown.
        Returns comprehensive metrics or None if data unavailable.
        """
        quote = await self.client.get_quote(ticker)
        if not quote:
            return None

        await self._load_benchmark()
        result = await self._process_ticker(ticker, quote)
        if result:
            score, signals, setup = self._score_opportunity(result)
            result["score"] = score
            result["signals"] = signals
            result["setup"] = setup
            result["change"] = quote.get("change", 0)
            result["change_pct"] = quote.get(
                "changesPercentage", quote.get("changePercentage", 0)
            )

        return result
