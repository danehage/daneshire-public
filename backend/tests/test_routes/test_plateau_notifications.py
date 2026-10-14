"""
Delivery tests for POST /api/internal/portfolio/run-plateau-checks.

The grade itself is covered by ``test_services/test_plateau_scoring.py``;
here the indicator layer is stubbed to a fixed ``Broken`` grade so the tests
pin only the notification bookkeeping: ``notified`` / ``fired`` follow the
Pushover send result, and the Pushover client is always closed. The ETF skip
and open-date resolution (issue #137) are pinned here too.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.portfolio import Account, HoldingGradeHistory
from app.models.watchlist import WatchlistItem
from app.routes.dependencies import get_portfolio_engine
from app.schemas.market import HistoryBar, HistoryFrame
from app.services import plateau
from app.services.market import UpstreamError, get_market
from app.services.portfolio_engine import ComputedHolding

TEST_SECRET = "test-scheduler-secret"
TICKER = "PLTX"
ETF_TICKER = "PLTE"  # profile says isEtf
UNKNOWN_TICKER = "PLTU"  # profile lookup fails
ALL_TICKERS = (TICKER, ETF_TICKER, UNKNOWN_TICKER)


@pytest.fixture(autouse=True)
async def setup_scheduler_secret():
    original = settings.scheduler_secret
    settings.scheduler_secret = TEST_SECRET
    yield
    settings.scheduler_secret = original


async def _wipe(db: AsyncSession):
    await db.execute(
        delete(HoldingGradeHistory).where(HoldingGradeHistory.ticker.in_(ALL_TICKERS))
    )
    await db.execute(delete(WatchlistItem).where(WatchlistItem.ticker.in_(ALL_TICKERS)))
    await db.execute(delete(Account).where(Account.name == "plateau-test"))
    await db.commit()


@pytest.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    await _wipe(db_session)
    yield
    await _wipe(db_session)
    app.dependency_overrides.pop(get_portfolio_engine, None)


class _FakePortfolioEngine:
    def __init__(self, tickers=(TICKER,)):
        self.tickers = tickers

    async def current_holdings(self, account_id):
        return [
            ComputedHolding(
                ticker=t,
                qty=Decimal("10"),
                avg_cost=Decimal("50"),
                instrument_type="equity",
            )
            for t in self.tickers
        ]

    async def latest_grades(self, tickers):
        return {}

    async def last_notified_at(self, tickers):
        return {}


class _BarsMarket:
    def __init__(self):
        self.history_calls: list[str] = []

    async def is_etf(self, ticker):
        if ticker == UNKNOWN_TICKER:
            raise UpstreamError(ticker, "profile request failed")
        return ticker == ETF_TICKER

    async def history(self, ticker, days=252):
        self.history_calls.append(ticker)
        bars = tuple(
            HistoryBar(
                date=f"2026-0{1 + i // 28}-{1 + i % 28:02d}",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000,
            )
            for i in range(80)
        )
        return HistoryFrame(ticker=ticker, bars=bars)


class _FakePushover:
    instances: list["_FakePushover"] = []

    def __init__(self, *, result=True, raises=None):
        self.result = result
        self.raises = raises
        self.sends = 0
        self.closed = False

    @property
    def is_configured(self):
        return True

    async def send(self, **kwargs):
        self.sends += 1
        if self.raises is not None:
            raise self.raises
        return self.result

    async def close(self):
        self.closed = True


def _install(monkeypatch, pushover: _FakePushover, tickers=(TICKER,)):
    """Wire the fakes in. Returns the fake market and the list of
    ``open_date`` values the grades were computed with."""
    open_dates: list = []

    def _metrics(**kwargs):
        open_dates.append(kwargs.get("open_date"))
        return {"atr14": None, "hv_now": None}

    market = _BarsMarket()
    monkeypatch.setattr(plateau, "PushoverClient", lambda: pushover)
    monkeypatch.setattr(
        plateau.TechnicalIndicators,
        "calculate_plateau_metrics",
        staticmethod(_metrics),
    )
    monkeypatch.setattr(
        plateau.TechnicalIndicators,
        "grade_holding",
        staticmethod(lambda metrics: ("Broken", 7, ["below 50MA"], None)),
    )
    app.dependency_overrides[get_portfolio_engine] = lambda: _FakePortfolioEngine(tickers)
    app.dependency_overrides[get_market] = lambda: market
    return market, open_dates


async def _seed_account(db: AsyncSession):
    db.add(Account(name="plateau-test", account_type="individual"))
    await db.commit()


async def _grade_rows(db: AsyncSession, ticker: str = TICKER) -> list[HoldingGradeHistory]:
    return list(
        (
            await db.execute(
                select(HoldingGradeHistory).where(HoldingGradeHistory.ticker == ticker)
            )
        ).scalars().all()
    )


@pytest.mark.asyncio
async def test_failed_push_is_not_marked_notified(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    await _seed_account(db_session)
    pushover = _FakePushover(result=False)
    _install(monkeypatch, pushover)

    resp = await client.post(
        "/api/internal/portfolio/run-plateau-checks",
        headers={"X-Scheduler-Secret": TEST_SECRET},
    )

    assert resp.status_code == 200
    assert resp.json()["notifications_sent"] == 0
    assert pushover.sends == 1
    assert pushover.closed is True
    rows = await _grade_rows(db_session)
    assert len(rows) == 1
    assert rows[0].notified is False


@pytest.mark.asyncio
async def test_delivered_push_is_marked_notified(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    await _seed_account(db_session)
    pushover = _FakePushover(result=True)
    _install(monkeypatch, pushover)

    resp = await client.post(
        "/api/internal/portfolio/run-plateau-checks",
        headers={"X-Scheduler-Secret": TEST_SECRET},
    )

    assert resp.status_code == 200
    assert resp.json()["notifications_sent"] == 1
    assert pushover.closed is True
    rows = await _grade_rows(db_session)
    assert len(rows) == 1
    assert rows[0].notified is True


@pytest.mark.asyncio
async def test_pushover_closed_when_run_raises(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    await _seed_account(db_session)
    pushover = _FakePushover(raises=RuntimeError("boom"))
    _install(monkeypatch, pushover)

    with pytest.raises(RuntimeError, match="boom"):
        await client.post(
            "/api/internal/portfolio/run-plateau-checks",
            headers={"X-Scheduler-Secret": TEST_SECRET},
        )

    assert pushover.closed is True


@pytest.mark.asyncio
async def test_etfs_skipped_stocks_graded_and_lookup_failure_still_grades(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, caplog
):
    """#137: an ETF is not graded, logged, notified or warned about; a stock
    is graded; a ticker whose profile lookup fails is graded as before."""
    await _seed_account(db_session)
    pushover = _FakePushover(result=True)
    market, _ = _install(monkeypatch, pushover, tickers=ALL_TICKERS)

    with caplog.at_level(logging.INFO, logger="app.services.plateau"):
        resp = await client.post(
            "/api/internal/portfolio/run-plateau-checks",
            headers={"X-Scheduler-Secret": TEST_SECRET},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["met"] == 2
    assert body["notifications_sent"] == 2
    assert pushover.sends == 2
    assert sorted(market.history_calls) == sorted([TICKER, UNKNOWN_TICKER])
    assert await _grade_rows(db_session, ETF_TICKER) == []
    assert len(await _grade_rows(db_session, TICKER)) == 1
    assert len(await _grade_rows(db_session, UNKNOWN_TICKER)) == 1

    no_date = [r.getMessage() for r in caplog.records if "no open date" in r.getMessage()]
    assert len(no_date) == 1
    assert ETF_TICKER not in no_date[0]
    assert TICKER in no_date[0]
    assert any("skipped_etf=1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_all_etf_holdings_grade_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    await _seed_account(db_session)
    pushover = _FakePushover(result=True)
    market, _ = _install(monkeypatch, pushover, tickers=(ETF_TICKER,))

    resp = await client.post(
        "/api/internal/portfolio/run-plateau-checks",
        headers={"X-Scheduler-Secret": TEST_SECRET},
    )

    assert resp.status_code == 200
    assert resp.json()["met"] == 0
    assert market.history_calls == []
    assert pushover.sends == 0


@pytest.mark.asyncio
async def test_entered_entry_date_is_the_open_date(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """#137: the date entered on the Portfolio page lands on the owned
    watchlist row's entry_date through the existing routes, and the plateau
    check anchors the run on it."""
    await _seed_account(db_session)
    created = await client.post(
        "/api/watchlist",
        json={"ticker": TICKER.lower(), "status": "owned"},
    )
    assert created.status_code == 201
    patched = await client.patch(
        f"/api/watchlist/{created.json()['id']}",
        json={"entry_date": "2026-02-03"},
    )
    assert patched.status_code == 200
    assert patched.json()["entry_date"] == "2026-02-03"

    _, open_dates = _install(monkeypatch, _FakePushover(result=True))
    resp = await client.post(
        "/api/internal/portfolio/run-plateau-checks",
        headers={"X-Scheduler-Secret": TEST_SECRET},
    )

    assert resp.status_code == 200
    assert open_dates == [str(date(2026, 2, 3))]
