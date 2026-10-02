"""DB-free tests for refresh-calendar's chunked Finnhub fetch.

Finnhub's /calendar/earnings truncates at 1500 rows; a single 28-day
request plateaued at exactly 1500 upserts during October earnings season.
``_fetch_calendar_chunked`` splits the window into weekly chunks and halves
any chunk that still hits the cap.
"""

import logging
from datetime import date, timedelta

import pytest

from app.routes.internal import (
    _FINNHUB_CALENDAR_ROW_CAP,
    _fetch_calendar_chunked,
)
from app.services.market import EarningsDateUnknown


class _RangeMarket:
    """Serves synthetic events per day, truncated at the Finnhub cap.

    ``per_day`` maps a date to how many events that day has (default
    ``default_per_day``). Every call is recorded as ``(start, end)``.
    """

    def __init__(self, per_day=None, default_per_day=1, fail_on=None, extra=None):
        self.per_day = per_day or {}
        self.default_per_day = default_per_day
        self.fail_on = fail_on
        self.extra = extra or []
        self.calls: list[tuple[date, date]] = []

    async def earnings_calendar(self, start: date, end: date):
        self.calls.append((start, end))
        if self.fail_on is not None and start <= self.fail_on <= end:
            raise EarningsDateUnknown("Finnhub unavailable")
        events = []
        d = start
        while d <= end:
            for i in range(self.per_day.get(d, self.default_per_day)):
                events.append({"symbol": f"T{i}", "date": d.isoformat(), "hour": "bmo"})
            d += timedelta(days=1)
        events.extend(self.extra)
        return events[:_FINNHUB_CALENDAR_ROW_CAP]


_START = date(2026, 10, 2)
_END = _START + timedelta(days=28)


def _covered_days(calls):
    days = []
    for start, end in calls:
        d = start
        while d <= end:
            days.append(d)
            d += timedelta(days=1)
    return days


async def test_chunks_cover_window_without_gaps_or_overlap():
    market = _RangeMarket()
    events = await _fetch_calendar_chunked(market, _START, _END)

    # 29 inclusive days → 7+7+7+7+1.
    assert len(market.calls) == 5
    assert all((e - s).days <= 6 for s, e in market.calls)
    assert market.calls[0][0] == _START
    assert market.calls[-1][1] == _END
    for (_, prev_end), (next_start, _) in zip(market.calls, market.calls[1:]):
        assert next_start == prev_end + timedelta(days=1)
    days = _covered_days(market.calls)
    assert len(days) == len(set(days)) == 29
    assert len(events) == 29


async def test_dedupes_by_symbol_and_date():
    dup = {"symbol": " aapl ", "date": "2026-10-30", "hour": "amc"}
    market = _RangeMarket(default_per_day=0, extra=[dup])

    events = await _fetch_calendar_chunked(market, _START, _END)

    # The same row comes back from every chunk; it survives once.
    assert len(market.calls) == 5
    assert events == [dup]


async def test_capped_chunk_is_halved_and_warned(caplog):
    # One heavy week: 300/day × 7 = 2100 rows → capped; halves fit.
    heavy = {_START + timedelta(days=i): 300 for i in range(7)}
    market = _RangeMarket(per_day=heavy)

    with caplog.at_level(logging.WARNING, logger="app.routes.internal"):
        events = await _fetch_calendar_chunked(market, _START, _END)

    assert len(events) == 2100 + 22
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "2026-10-02" in msg and "2026-10-08" in msg
    # First week fetched whole, then as 10-02..10-05 + 10-06..10-08.
    assert market.calls[:3] == [
        (_START, date(2026, 10, 8)),
        (_START, date(2026, 10, 5)),
        (date(2026, 10, 6), date(2026, 10, 8)),
    ]


async def test_single_day_at_cap_is_kept_with_warning(caplog):
    market = _RangeMarket(per_day={_START: 5000}, default_per_day=0)

    with caplog.at_level(logging.WARNING, logger="app.routes.internal"):
        events = await _fetch_calendar_chunked(market, _START, _START + timedelta(days=6))

    assert len(events) == _FINNHUB_CALENDAR_ROW_CAP
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("single day 2026-10-02" in m for m in msgs)
    # Recursion bottoms out at one day; no endless refetching.
    assert (_START, _START) in market.calls
    assert len(market.calls) < 10


async def test_failed_chunk_propagates():
    market = _RangeMarket(fail_on=_START + timedelta(days=15))

    with pytest.raises(EarningsDateUnknown):
        await _fetch_calendar_chunked(market, _START, _END)
