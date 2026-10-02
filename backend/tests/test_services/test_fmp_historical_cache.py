"""
Tests for FMPClient.get_historical_data's daily cache.

The cache used to store only the first caller's ``days`` and return the
oldest N bars on a hit, so a 700-day plateau request followed by a
252-day scanner request served bars ~2-3 years stale. These tests pin the
fixed contract: whatever is requested, in whatever order, the result is
oldest -> newest, at most ``days`` long, and ends at the newest bar.

No network calls — `_request_with_retry` is replaced on the instance.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from app.services.fmp_client import FMPClient

TOTAL_BARS = 1255  # roughly what FMP's full EOD endpoint returns (~5y)
NEWEST = date(2026, 10, 1)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def fmp_payload(n: int = TOTAL_BARS) -> list[dict]:
    """FMP-shaped history: newest first."""
    return [
        {"date": (NEWEST - timedelta(days=i)).isoformat(), "close": 100.0 + i}
        for i in range(n)
    ]


def make_client(tmp_path, payload=None):
    client = FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp_cache"))
    calls: list[str] = []
    body = payload if payload is not None else fmp_payload()

    async def fake_request(url, http_client, retries=3):
        calls.append(url)
        return FakeResponse(body)

    client._request_with_retry = fake_request
    return client, calls


def assert_newest_window(hist: list[dict], days: int):
    assert len(hist) == days
    assert hist[-1]["date"] == NEWEST.isoformat()
    dates = [bar["date"] for bar in hist]
    assert dates == sorted(dates)  # oldest -> newest


async def test_fetch_returns_newest_window(tmp_path):
    client, calls = make_client(tmp_path)

    hist = await client.get_historical_data("AAPL", days=252)

    assert_newest_window(hist, 252)
    assert len(calls) == 1


async def test_long_then_short_request_serves_newest_bars_from_cache(tmp_path):
    client, calls = make_client(tmp_path)

    first = await client.get_historical_data("AAPL", days=700)
    second = await client.get_historical_data("AAPL", days=252)

    assert_newest_window(first, 700)
    assert_newest_window(second, 252)
    assert second == first[-252:]
    assert len(calls) == 1  # second request was a cache hit


async def test_short_then_long_request_returns_full_window(tmp_path):
    client, calls = make_client(tmp_path)

    first = await client.get_historical_data("AAPL", days=252)
    second = await client.get_historical_data("AAPL", days=700)
    third = await client.get_historical_data("AAPL", days=1500)

    assert_newest_window(first, 252)
    assert_newest_window(second, 700)
    # More than FMP has: everything available, still ending at the newest bar.
    assert len(third) == TOTAL_BARS
    assert third[-1]["date"] == NEWEST.isoformat()
    # The cache holds the full series, so no refetch was needed.
    assert len(calls) == 1


async def test_cache_file_holds_full_series(tmp_path):
    client, _ = make_client(tmp_path)

    await client.get_historical_data("AAPL", days=252)

    with open(client._get_hist_cache_path("AAPL")) as f:
        cached = json.load(f)
    assert len(cached) == TOTAL_BARS
    assert cached[-1]["date"] == NEWEST.isoformat()


async def test_legacy_truncated_cache_file_is_ignored(tmp_path):
    """A pre-fix ``hist_`` file (first caller's days only) must not be read."""
    client, calls = make_client(tmp_path)
    legacy = list(reversed(fmp_payload()[-252:]))  # oldest 252 bars, as old code stored
    today = os.path.basename(client._get_hist_cache_path("AAPL")).split("_")[-1]
    with open(os.path.join(client.cache_dir, f"hist_AAPL_{today}"), "w") as f:
        json.dump(legacy, f)

    hist = await client.get_historical_data("AAPL", days=252)

    assert_newest_window(hist, 252)
    assert len(calls) == 1


async def test_wrapped_historical_payload(tmp_path):
    client, _ = make_client(tmp_path, payload={"historical": fmp_payload()})

    hist = await client.get_historical_data("AAPL", days=700)

    assert_newest_window(hist, 700)


async def test_short_history_ticker_returns_everything(tmp_path):
    client, calls = make_client(tmp_path, payload=fmp_payload(100))

    first = await client.get_historical_data("NEWIPO", days=252)
    second = await client.get_historical_data("NEWIPO", days=252)

    assert len(first) == len(second) == 100
    assert second[-1]["date"] == NEWEST.isoformat()
    assert len(calls) == 1
