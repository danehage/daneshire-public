"""
Stage-1 cache must never feed stale quotes into Stage 2.

The cache stores only the survivor ticker list; a cache hit refetches fresh
quotes for those survivors (one bulk call) so Stage 2 scores current
price/volume. DB-free and network-free: a fake client stands in for FMP and
``_process_ticker`` is replaced to capture the quote Stage 2 receives.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytz

from app.services.scanner import StockScanner, universe_digest


def _quote(price: float, volume: int) -> dict:
    return {"price": price, "volume": volume, "marketCap": 5_000_000_000}


class FakeClient:
    """Serves whatever ``self.quotes`` holds at call time; records calls."""

    def __init__(self, quotes: dict[str, dict]):
        self.quotes = quotes
        self.batch_calls: list[list[str]] = []

    async def get_batch_quotes(self, tickers, max_concurrent=10, on_progress=None):
        self.batch_calls.append(list(tickers))
        return {t: dict(self.quotes[t]) for t in tickers if t in self.quotes}


def _make_scanner(tmp_path, client) -> tuple[StockScanner, dict[str, dict]]:
    scanner = StockScanner(cache_dir=str(tmp_path / "scan_cache"), client=client)
    seen: dict[str, dict] = {}

    async def capture(ticker, quote):
        seen[ticker] = quote
        return None

    scanner._process_ticker = capture
    return scanner, seen


async def test_cached_scan_uses_fresh_quotes(tmp_path):
    universe = ["AAA", "BBB", "PENNY"]
    client = FakeClient(
        {
            "AAA": _quote(100.0, 200_000),  # 9:45am
            "BBB": _quote(50.0, 300_000),
            "PENNY": _quote(5.0, 900_000),  # fails Stage 1 (price < 20)
        }
    )
    scanner, seen = _make_scanner(tmp_path, client)

    await scanner.run_scan(universe, use_cache=True, universe="sp500")
    assert seen["AAA"]["price"] == 100.0
    assert "PENNY" not in seen

    # The cache file holds tickers only -- no quote values to go stale --
    # plus the fingerprint of the universe they were cut from.
    (cache_file,) = os.listdir(tmp_path / "scan_cache")
    with open(tmp_path / "scan_cache" / cache_file) as f:
        assert json.load(f) == {
            "tickers": ["AAA", "BBB"],
            "universe_digest": universe_digest(universe),
        }

    # Hours later: prices and cumulative volume have moved.
    client.quotes["AAA"] = _quote(92.5, 4_100_000)
    client.quotes["BBB"] = _quote(51.0, 2_600_000)
    client.batch_calls.clear()
    seen.clear()

    await scanner.run_scan(universe, use_cache=True, universe="sp500")

    # One bulk refetch, survivors only -- not the whole universe.
    assert client.batch_calls == [["AAA", "BBB"]]
    assert seen["AAA"] == _quote(92.5, 4_100_000)
    assert seen["BBB"] == _quote(51.0, 2_600_000)
    assert "PENNY" not in seen


async def test_cache_hit_reapplies_stage1_filter(tmp_path):
    client = FakeClient({"AAA": _quote(100.0, 200_000), "BBB": _quote(50.0, 300_000)})
    scanner, seen = _make_scanner(tmp_path, client)
    await scanner.run_scan(["AAA", "BBB"], use_cache=True, universe="sp500")

    client.quotes["BBB"] = _quote(18.0, 300_000)  # dropped below the $20 floor
    seen.clear()
    await scanner.run_scan(["AAA", "BBB"], use_cache=True, universe="sp500")

    assert set(seen) == {"AAA"}


async def test_legacy_quote_cache_is_not_reused(tmp_path):
    """A same-day file in the old ticker->quote format must not leak its
    quotes; it has no universe digest, so it isn't reused at all."""
    client = FakeClient({"AAA": _quote(92.5, 4_100_000)})
    scanner, seen = _make_scanner(tmp_path, client)
    scanner._save_cache("stage1_sp500", {"AAA": _quote(100.0, 200_000)})

    await scanner.run_scan(["AAA"], use_cache=True, universe="sp500")

    assert client.batch_calls == [["AAA"]]
    assert seen["AAA"]["price"] == 92.5


def test_cache_date_key_is_eastern(tmp_path):
    scanner = StockScanner(cache_dir=str(tmp_path / "c"), client=FakeClient({}))
    today_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    assert scanner._get_cache_path("stage1_x").endswith(f"stage1_x_{today_et}.json")


async def test_cache_from_a_different_universe_is_not_reused(tmp_path):
    """``combined`` is a screener build since #118: a survivor list cut
    from last week's build (or the static fallback) must not stand in for
    today's, even under the same universe name and date."""
    client = FakeClient(
        {"AAA": _quote(100.0, 200_000), "BBB": _quote(50.0, 300_000), "NEW": _quote(80.0, 900_000)}
    )
    scanner, seen = _make_scanner(tmp_path, client)
    await scanner.run_scan(["AAA", "BBB"], use_cache=True, universe="combined_screener")

    client.batch_calls.clear()
    seen.clear()
    await scanner.run_scan(["AAA", "BBB", "NEW"], use_cache=True, universe="combined_screener")

    # Whole new universe fetched, not just the old survivors.
    assert client.batch_calls == [["AAA", "BBB", "NEW"]]
    assert set(seen) == {"AAA", "BBB", "NEW"}


def test_universe_digest_ignores_order():
    assert universe_digest(["B", "A"]) == universe_digest(["A", "B"])
    assert universe_digest(["A"]) != universe_digest(["A", "B"])


def _recorder():
    events: list[dict] = []

    async def record(event):
        events.append(event)

    return events, record


def _stages(events: list[dict]) -> list[tuple]:
    return [
        (e["stage"], e["current"], e["total"])
        for e in events
        if e["type"] == "progress"
    ]


async def test_progress_covers_the_quote_stage_then_analysis(tmp_path):
    universe = ["AAA", "BBB", "PENNY"]
    client = FakeClient(
        {
            "AAA": _quote(100.0, 200_000),
            "BBB": _quote(50.0, 300_000),
            "PENNY": _quote(5.0, 900_000),  # fails Stage 1
        }
    )
    scanner, _ = _make_scanner(tmp_path, client)
    events, record = _recorder()

    await scanner.run_scan(
        universe, use_cache=False, universe="sp500", progress_callback=record
    )

    # Quotes count the whole universe; analysis counts the survivors.
    assert _stages(events) == [
        ("quotes", 0, 3),
        ("analysis", 0, 2),
        ("analysis", 1, 2),
        ("analysis", 2, 2),
    ]


async def test_cache_hit_reports_quote_progress_over_the_survivors(tmp_path):
    universe = ["AAA", "BBB", "PENNY"]
    client = FakeClient(
        {
            "AAA": _quote(100.0, 200_000),
            "BBB": _quote(50.0, 300_000),
            "PENNY": _quote(5.0, 900_000),
        }
    )
    scanner, _ = _make_scanner(tmp_path, client)
    await scanner.run_scan(universe, use_cache=True, universe="sp500")

    events, record = _recorder()
    await scanner.run_scan(
        universe, use_cache=True, universe="sp500", progress_callback=record
    )

    assert _stages(events)[0] == ("quotes", 0, 2)
    assert _stages(events)[1] == ("analysis", 0, 2)


async def test_quote_stage_forwards_client_progress(tmp_path):
    class ReportingClient(FakeClient):
        async def get_batch_quotes(self, tickers, max_concurrent=10, on_progress=None):
            await on_progress(1, len(tickers))
            return await super().get_batch_quotes(tickers)

    client = ReportingClient({"AAA": _quote(100.0, 200_000), "BBB": _quote(50.0, 300_000)})
    scanner, _ = _make_scanner(tmp_path, client)
    events, record = _recorder()

    await scanner.run_scan(
        ["AAA", "BBB"], use_cache=False, universe="sp500", progress_callback=record
    )

    assert _stages(events)[:2] == [("quotes", 0, 2), ("quotes", 1, 2)]
