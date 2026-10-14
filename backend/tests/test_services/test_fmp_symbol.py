"""
FMP symbol spelling for US share classes.

Brokers (and so portfolio holdings) write BRK.B; FMP answers HTTP 402 for
the dotted form on our plan and serves BRK-B. The plateau check skipped
BRK.B every run because of this (2026-10-13 logs). No network calls.
"""

from __future__ import annotations

import pytest

from app.services.fmp_client import FMPClient, fmp_symbol


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("BRK.B", "BRK-B"),
        ("brk.b", "BRK-B"),
        ("BF.B", "BF-B"),
        ("MOG.A", "MOG-A"),
        ("BRK-B", "BRK-B"),  # already FMP spelling
        ("AAPL", "AAPL"),
        ("SHOP.TO", "SHOP.TO"),  # exchange suffix, not a share class
    ],
)
def test_fmp_symbol(ticker, expected):
    assert fmp_symbol(ticker) == expected


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def make_client(tmp_path, payload):
    client = FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp_cache"))
    urls: list[str] = []

    async def fake_request(url, http_client, retries=3):
        urls.append(url)
        return FakeResponse(payload)

    client._request_with_retry = fake_request
    return client, urls


async def test_history_requests_dashed_symbol(tmp_path):
    client, urls = make_client(tmp_path, [{"date": "2026-10-13", "close": 480.0}])

    hist = await client.get_historical_data("BRK.B", days=252)

    assert hist and hist[-1]["close"] == 480.0
    assert "symbol=BRK-B" in urls[0]
    assert "BRK.B" not in urls[0]


async def test_batch_quotes_request_dashed_and_key_by_requested_spelling(tmp_path):
    client, urls = make_client(
        tmp_path,
        [{"symbol": "BRK-B", "price": 480.0}, {"symbol": "AAPL", "price": 250.0}],
    )

    quotes = await client.get_batch_quotes(["BRK.B", "AAPL"])

    assert "BRK-B" in urls[0] and "BRK.B" not in urls[0]
    assert quotes["BRK.B"]["price"] == 480.0
    assert quotes["AAPL"]["price"] == 250.0
