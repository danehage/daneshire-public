"""Tests for FMPClient.get_earnings_date against /stable/earnings.

The legacy /api/v3/earning_calendar died at FMP's 2025-08-31 cutoff and
silently returned None for every ticker (error payload, not a list). The
stable endpoint returns entries date-DESCENDING, so the nearest upcoming
date must be selected explicitly — taking the first future entry returns
the farthest-out quarter instead.

Dates are derived from today so these never rot on the calendar.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.services.fmp_client import FMPClient


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def make_client(tmp_path) -> FMPClient:
    return FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp_cache"))


def _patch_response(monkeypatch, payload):
    async def fake_get(self, url, *args, **kwargs):
        return FakeResponse(payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_picks_nearest_upcoming_from_descending_list(tmp_path, monkeypatch):
    today = date.today()
    far = today + timedelta(days=110)
    near = today + timedelta(days=20)
    past = today - timedelta(days=70)
    _patch_response(
        monkeypatch,
        [
            {"symbol": "AAPL", "date": far.isoformat(), "epsActual": None},
            {"symbol": "AAPL", "date": near.isoformat(), "epsActual": None},
            {"symbol": "AAPL", "date": past.isoformat(), "epsActual": 2.01},
        ],
    )

    result = await make_client(tmp_path).get_earnings_date("AAPL")

    assert result == {"date": near.isoformat(), "days_until": 20}


async def test_returns_none_when_only_past_dates(tmp_path, monkeypatch):
    today = date.today()
    _patch_response(
        monkeypatch,
        [
            {"symbol": "AAPL", "date": (today - timedelta(days=10)).isoformat()},
            {"symbol": "AAPL", "date": (today - timedelta(days=100)).isoformat()},
        ],
    )

    assert await make_client(tmp_path).get_earnings_date("AAPL") is None


async def test_returns_none_on_error_payload(tmp_path, monkeypatch):
    # The exact failure mode the legacy endpoint produced: a dict, not a list.
    _patch_response(monkeypatch, {"Error Message": "Legacy Endpoint ..."})

    assert await make_client(tmp_path).get_earnings_date("AAPL") is None


async def test_earnings_info_carries_last_report_from_same_call(tmp_path, monkeypatch):
    # #117: the last report's EPS comes from the response the scanner
    # already fetches for the next date -- no extra request.
    today = date.today()
    near = today + timedelta(days=20)
    last = today - timedelta(days=70)
    older = today - timedelta(days=160)
    _patch_response(
        monkeypatch,
        [
            {"symbol": "AAPL", "date": near.isoformat(), "epsActual": None, "epsEstimated": 1.5},
            {"symbol": "AAPL", "date": last.isoformat(), "epsActual": 2.1, "epsEstimated": 2.0},
            {"symbol": "AAPL", "date": older.isoformat(), "epsActual": 1.0, "epsEstimated": 1.2},
        ],
    )

    info = await make_client(tmp_path).get_earnings_info("AAPL")

    assert info == {
        "next_date": near.isoformat(),
        "days_until": 20,
        "last_date": last.isoformat(),
        "last_eps_actual": 2.1,
        "last_eps_estimate": 2.0,
    }


async def test_earnings_info_skips_past_rows_without_actuals(tmp_path, monkeypatch):
    today = date.today()
    _patch_response(
        monkeypatch,
        [
            {"symbol": "AAPL", "date": (today - timedelta(days=5)).isoformat(), "epsActual": None},
            {"symbol": "AAPL", "date": (today - timedelta(days=95)).isoformat(),
             "epsActual": "0.9", "epsEstimated": None},
        ],
    )

    info = await make_client(tmp_path).get_earnings_info("AAPL")

    assert info["next_date"] is None and info["days_until"] is None
    assert info["last_date"] == (today - timedelta(days=95)).isoformat()
    assert info["last_eps_actual"] == 0.9
    assert info["last_eps_estimate"] is None
