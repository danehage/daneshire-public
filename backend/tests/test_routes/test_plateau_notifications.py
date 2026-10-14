"""
Delivery tests for POST /api/internal/portfolio/run-plateau-checks.

The grade itself is covered by ``test_services/test_plateau_scoring.py``;
here the indicator layer is stubbed to a fixed ``Broken`` grade so the tests
pin only the notification bookkeeping: ``notified`` / ``fired`` follow the
Pushover send result, and the Pushover client is always closed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.portfolio import Account, HoldingGradeHistory
from app.routes.dependencies import get_portfolio_engine
from app.schemas.market import HistoryBar, HistoryFrame
from app.services import plateau
from app.services.market import get_market
from app.services.portfolio_engine import ComputedHolding

TEST_SECRET = "test-scheduler-secret"
TICKER = "PLTX"


@pytest.fixture(autouse=True)
async def setup_scheduler_secret():
    original = settings.scheduler_secret
    settings.scheduler_secret = TEST_SECRET
    yield
    settings.scheduler_secret = original


@pytest.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    await db_session.execute(
        delete(HoldingGradeHistory).where(HoldingGradeHistory.ticker == TICKER)
    )
    await db_session.execute(delete(Account).where(Account.name == "plateau-test"))
    await db_session.commit()
    yield
    await db_session.execute(
        delete(HoldingGradeHistory).where(HoldingGradeHistory.ticker == TICKER)
    )
    await db_session.execute(delete(Account).where(Account.name == "plateau-test"))
    await db_session.commit()
    app.dependency_overrides.pop(get_portfolio_engine, None)


class _FakePortfolioEngine:
    async def current_holdings(self, account_id):
        return [
            ComputedHolding(
                ticker=TICKER,
                qty=Decimal("10"),
                avg_cost=Decimal("50"),
                instrument_type="equity",
            )
        ]

    async def latest_grades(self, tickers):
        return {}

    async def last_notified_at(self, tickers):
        return {}


class _BarsMarket:
    async def history(self, ticker, days=252):
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


def _install(monkeypatch, pushover: _FakePushover):
    monkeypatch.setattr(plateau, "PushoverClient", lambda: pushover)
    monkeypatch.setattr(
        plateau.TechnicalIndicators,
        "calculate_plateau_metrics",
        staticmethod(lambda **kwargs: {"atr14": None, "hv_now": None}),
    )
    monkeypatch.setattr(
        plateau.TechnicalIndicators,
        "grade_holding",
        staticmethod(lambda metrics: ("Broken", 7, ["below 50MA"], None)),
    )
    app.dependency_overrides[get_portfolio_engine] = lambda: _FakePortfolioEngine()
    app.dependency_overrides[get_market] = lambda: _BarsMarket()


async def _seed_account(db: AsyncSession):
    db.add(Account(name="plateau-test", account_type="individual"))
    await db.commit()


async def _grade_rows(db: AsyncSession) -> list[HoldingGradeHistory]:
    return list(
        (
            await db.execute(
                select(HoldingGradeHistory).where(HoldingGradeHistory.ticker == TICKER)
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
