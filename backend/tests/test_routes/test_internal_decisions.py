"""POST /api/internal/decisions/score (#119): auth, end-to-end scoring
against Postgres with a fake MarketData, idempotency, and the no_data /
outage paths. The scoring rules themselves are unit-tested in
tests/test_services/test_decision_scoring.py."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import pytz
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.decision import Decision, DecisionOutcome
from app.routes.internal import get_now_et
from app.schemas.market import HistoryBar, HistoryFrame
from app.services.market import TickerNotFound, UpstreamError, get_market

TEST_SECRET = "test-scheduler-secret"
HEADERS = {"X-Scheduler-Secret": TEST_SECRET}
ET = pytz.timezone("America/New_York")

DECIDED_AT = ET.localize(datetime(2026, 3, 2, 11, 0))  # Monday, mid-session


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DATES = _weekdays(date(2026, 2, 20), date(2026, 12, 31))


def _frame(ticker: str, base: float, step: float) -> HistoryFrame:
    return HistoryFrame(
        ticker=ticker,
        bars=tuple(
            HistoryBar(date=d.isoformat(), close=base + step * i) for i, d in enumerate(DATES)
        ),
    )


class _HistoryMarket:
    def __init__(self, frames=None, errors=None, upstream_failure=None):
        self.frames = frames or {}
        self.errors = errors or {}
        self._failure = upstream_failure
        self.calls: list[str] = []

    async def history_uncached(self, ticker, days=252):
        self.calls.append(ticker)
        if ticker in self.errors:
            raise self.errors[ticker]
        if ticker not in self.frames:
            raise TickerNotFound(ticker)
        return self.frames[ticker]

    def upstream_failure(self):
        return self._failure


def _session(n: int) -> date:
    """Date of SPY session n after DECIDED_AT (session 1 = 2026-03-02)."""
    return [d for d in DATES if d >= date(2026, 3, 2)][n - 1]


def _evening(d: date) -> datetime:
    return ET.localize(datetime(d.year, d.month, d.day, 18, 30))


@pytest.fixture(autouse=True)
def scheduler_secret(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_secret", TEST_SECRET)


@pytest.fixture
def pin_clock():
    def _pin(now_et: datetime):
        app.dependency_overrides[get_now_et] = lambda: now_et

    yield _pin
    app.dependency_overrides.pop(get_now_et, None)


def _use_market(market):
    app.dependency_overrides[get_market] = lambda: market


# ---------------------------------------------------------------------------
# Auth: no DB needed, the secret check rejects first
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bare_client():
    async def no_db():
        raise AssertionError("the database must not be touched without the secret")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = no_db
    app.dependency_overrides[get_market] = lambda: _HistoryMarket()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_score_requires_the_scheduler_secret(bare_client: AsyncClient):
    assert (await bare_client.post("/api/internal/decisions/score")).status_code == 401
    wrong = await bare_client.post(
        "/api/internal/decisions/score", headers={"X-Scheduler-Secret": "nope"}
    )
    assert wrong.status_code == 401


async def test_score_is_not_reachable_with_basic_auth_alone(bare_client, monkeypatch):
    """/api/internal/* bypasses Basic Auth; owner credentials don't stand in
    for the secret."""
    import base64

    monkeypatch.setattr(settings, "auth_username", "dane")
    monkeypatch.setattr(settings, "auth_password", "pw")
    token = base64.b64encode(b"dane:pw").decode()
    response = await bare_client.post(
        "/api/internal/decisions/score", headers={"Authorization": f"Basic {token}"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end against Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clean_db(db_session: AsyncSession):
    async def wipe():
        await db_session.execute(delete(DecisionOutcome))
        await db_session.execute(delete(Decision))
        await db_session.commit()

    await wipe()
    yield db_session
    await db_session.rollback()
    await wipe()


async def _seed(db: AsyncSession, ticker="DLGA", created_at=DECIDED_AT) -> Decision:
    d = Decision(
        ticker=ticker,
        decision="buy",
        conviction=3,
        thesis="t",
        invalidation="i",
        price_at_decision=Decimal("100"),
        spy_at_decision=Decimal("500"),
        source="manual",
        created_at=created_at,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


async def _outcomes(db: AsyncSession, decision_id) -> list[DecisionOutcome]:
    # populate_existing rather than expire_all: expiring would turn the
    # seeded Decision's attributes into lazy loads, which async can't do.
    rows = await db.execute(
        select(DecisionOutcome)
        .where(DecisionOutcome.decision_id == decision_id)
        .order_by(DecisionOutcome.horizon_sessions)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


SPY_FRAME = _frame("SPY", 490.0, 1.0)
DLGA_FRAME = _frame("DLGA", 95.0, 0.5)


async def test_scores_all_due_horizons_then_is_idempotent(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db)
    _use_market(_HistoryMarket({"SPY": SPY_FRAME, "DLGA": DLGA_FRAME}))
    pin_clock(_evening(_session(140)))

    first = await client.post("/api/internal/decisions/score", headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["scored"] == 3
    assert first.json()["pending"] == 0

    rows = await _outcomes(clean_db, d.id)
    assert [(r.horizon_sessions, r.status) for r in rows] == [
        (21, "scored"), (63, "scored"), (126, "scored"),
    ]
    assert [r.exit_date for r in rows] == [_session(21), _session(63), _session(126)]
    spy_close = {date.fromisoformat(b.date): b.close for b in SPY_FRAME.bars}
    tkr_close = {date.fromisoformat(b.date): b.close for b in DLGA_FRAME.bars}
    exit21 = _session(21)
    assert float(rows[0].ticker_return) == pytest.approx(tkr_close[exit21] / 100 - 1, abs=1e-6)
    assert float(rows[0].spy_return) == pytest.approx(spy_close[exit21] / 500 - 1, abs=1e-6)
    assert float(rows[0].excess_return) == pytest.approx(
        float(rows[0].ticker_return) - float(rows[0].spy_return), abs=2e-6
    )

    # A scheduler retry or a manual re-run writes nothing new.
    second = await client.post("/api/internal/decisions/score", headers=HEADERS)
    assert second.status_code == 200
    assert second.json()["decisions_checked"] == 0
    assert second.json()["scored"] == 0
    count = await clean_db.execute(
        select(func.count()).select_from(DecisionOutcome).where(DecisionOutcome.decision_id == d.id)
    )
    assert count.scalar_one() == 3


async def test_missed_days_catch_up_one_horizon_at_a_time(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db)
    _use_market(_HistoryMarket({"SPY": SPY_FRAME, "DLGA": DLGA_FRAME}))

    pin_clock(_evening(_session(20)))
    early = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert early["scored"] == 0 and early["pending"] == 3

    pin_clock(_evening(_session(30)))
    mid = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert mid["scored"] == 1 and mid["pending"] == 2

    pin_clock(_evening(_session(130)))
    late = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert late["scored"] == 2 and late["pending"] == 0
    assert [r.horizon_sessions for r in await _outcomes(clean_db, d.id)] == [21, 63, 126]


async def test_run_during_the_exit_session_waits_for_the_close(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    """No look-ahead: the exit day's bar isn't used until it has settled,
    even though the fake series already contains it."""
    d = await _seed(clean_db)
    _use_market(_HistoryMarket({"SPY": SPY_FRAME, "DLGA": DLGA_FRAME}))
    exit_day = _session(21)

    pin_clock(ET.localize(datetime(exit_day.year, exit_day.month, exit_day.day, 12, 0)))
    assert (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()["scored"] == 0

    pin_clock(_evening(exit_day))
    assert (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()["scored"] == 1
    assert (await _outcomes(clean_db, d.id))[0].exit_date == exit_day


async def test_no_data_after_ten_sessions_past_due(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db, ticker="GONE")
    _use_market(_HistoryMarket({"SPY": SPY_FRAME}))  # GONE -> TickerNotFound

    pin_clock(_evening(_session(30)))  # 9 sessions past the 21 exit
    assert (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()["no_data"] == 0

    pin_clock(_evening(_session(31)))
    body = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert body["no_data"] == 1
    rows = await _outcomes(clean_db, d.id)
    assert [(r.horizon_sessions, r.status, r.excess_return) for r in rows] == [(21, "no_data", None)]


async def test_truncated_when_the_ticker_stops_trading(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db, ticker="DLST")
    last_day = _session(40)
    delisted = HistoryFrame(
        ticker="DLST",
        bars=tuple(b for b in DLGA_FRAME.bars if date.fromisoformat(b.date) <= last_day),
    )
    _use_market(_HistoryMarket({"SPY": SPY_FRAME, "DLST": delisted}))
    pin_clock(_evening(_session(140)))

    body = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert body["scored"] == 1 and body["truncated"] == 2
    rows = await _outcomes(clean_db, d.id)
    assert [r.status for r in rows] == ["scored", "truncated", "truncated"]
    assert rows[1].exit_date == rows[2].exit_date == last_day


async def test_provider_outage_never_runs_down_the_no_data_clock(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db)
    _use_market(
        _HistoryMarket({"SPY": SPY_FRAME}, upstream_failure="HTTP 500 server error")
    )
    pin_clock(_evening(_session(140)))

    body = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert body["tickers_unavailable"] == ["DLGA"]
    assert body["no_data"] == 0 and body["pending"] == 3
    assert await _outcomes(clean_db, d.id) == []


async def test_upstream_error_for_one_ticker_leaves_the_rest_scored(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    ok = await _seed(clean_db, ticker="DLGA")
    bad = await _seed(clean_db, ticker="DLGB")
    _use_market(
        _HistoryMarket(
            {"SPY": SPY_FRAME, "DLGA": DLGA_FRAME},
            errors={"DLGB": UpstreamError("DLGB", "timeout")},
        )
    )
    pin_clock(_evening(_session(140)))

    body = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert body["scored"] == 3 and body["tickers_unavailable"] == ["DLGB"]
    assert len(await _outcomes(clean_db, ok.id)) == 3
    assert await _outcomes(clean_db, bad.id) == []


async def test_spy_unavailable_is_502_and_writes_nothing(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    d = await _seed(clean_db)
    _use_market(
        _HistoryMarket({"DLGA": DLGA_FRAME}, errors={"SPY": UpstreamError("SPY", "down")})
    )
    pin_clock(_evening(_session(140)))

    response = await client.post("/api/internal/decisions/score", headers=HEADERS)
    assert response.status_code == 502
    assert await _outcomes(clean_db, d.id) == []


async def test_nothing_pending_makes_no_market_calls(
    client: AsyncClient, clean_db: AsyncSession, pin_clock
):
    market = _HistoryMarket()
    _use_market(market)
    pin_clock(_evening(_session(140)))
    body = (await client.post("/api/internal/decisions/score", headers=HEADERS)).json()
    assert body["decisions_checked"] == 0
    assert market.calls == []
