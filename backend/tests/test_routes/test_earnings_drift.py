"""
Regression tests for earnings date-drift handling (#75).

Covers:
- Simple drift: a moved date updates the existing future row in place.
- Drift onto an existing duplicate (already-created pair): merges them.
- Past-event immutability: reported past events (realized move set, or
  outside the ±14-day window) are never modified by drift handling.
- Date slipped into the past: an unrealized past row inside the window is
  moved, not duplicated.
- Null fiscal_period: falls back to ±14-day window.
- Trade FK preservation: linked trades survive a drift update intact.
- merge-duplicate-events endpoint: merges stale pairs (including fully-past
  pairs like the prod IR/ROL duplicates), idempotent.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.earnings import EarningsEvent
from app.models.earnings_trades import EarningsTrade
from app.services.market import EarningsDateUnknown, get_market

TEST_SECRET = "test-scheduler-secret-drift"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date.today()  # drift logic compares against the real clock


def auth_headers() -> dict:
    return {"X-Scheduler-Secret": TEST_SECRET}


class _FakeMarket:
    """Minimal market stub — only earnings_calendar is exercised here."""

    def __init__(self, events: list[dict]):
        self._events = events

    async def earnings_calendar(self, start, end):
        return list(self._events)

    async def analyses(self, tickers):
        return {}

    async def quotes(self, tickers):
        return {}

    async def analysis(self, ticker):
        raise RuntimeError("not expected")

    async def quote(self, ticker):
        raise RuntimeError("not expected")

    async def history(self, ticker, days=252):
        raise RuntimeError("not expected")


def _set_market(market) -> None:
    app.dependency_overrides[get_market] = lambda: market


def _clear_market() -> None:
    app.dependency_overrides.pop(get_market, None)


def _raw_event(
    symbol: str,
    report_date: date,
    hour: str = "amc",
    year: Optional[int] = None,
    quarter: Optional[int] = None,
) -> dict:
    e: dict = {"symbol": symbol, "date": report_date.isoformat(), "hour": hour}
    if year is not None:
        e["year"] = year
    if quarter is not None:
        e["quarter"] = quarter
    return e


async def _insert_event(
    db: AsyncSession,
    ticker: str,
    report_date: date,
    fiscal_period: Optional[str] = "Q3 2026",
    report_time: str = "amc",
    realized_move_pct=None,
) -> UUID:
    result = await db.execute(
        insert(EarningsEvent)
        .values(
            ticker=ticker,
            report_date=report_date,
            report_time=report_time,
            fiscal_period=fiscal_period,
            source="finnhub",
            realized_move_pct=realized_move_pct,
        )
        .returning(EarningsEvent.id)
    )
    event_id: UUID = result.scalar_one()
    await db.commit()
    return event_id


async def _insert_trade(db: AsyncSession, event_id: UUID) -> UUID:
    result = await db.execute(
        insert(EarningsTrade)
        .values(
            ticker="AAPL",
            earnings_event_id=event_id,
            structure="iron_condor",
            is_paper=True,
            entry_date=TODAY - timedelta(days=9),
            expiry_date=TODAY + timedelta(days=5),
            short_put_strike="170.00",
            long_put_strike="165.00",
            short_call_strike="190.00",
            long_call_strike="195.00",
            entry_credit="1.50",
            contracts=1,
        )
        .returning(EarningsTrade.id)
    )
    trade_id: UUID = result.scalar_one()
    await db.commit()
    return trade_id


async def _event_count(db: AsyncSession, ticker: str) -> int:
    row = await db.execute(
        select(func.count()).select_from(EarningsEvent).where(EarningsEvent.ticker == ticker)
    )
    return int(row.scalar() or 0)


async def _fetch_event(db: AsyncSession, event_id: UUID) -> Optional[EarningsEvent]:
    return (await db.get(EarningsEvent, event_id))


async def _fetch_trade(db: AsyncSession, trade_id: UUID) -> Optional[EarningsTrade]:
    return (await db.get(EarningsTrade, trade_id))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_scheduler_secret():
    original = settings.scheduler_secret
    settings.scheduler_secret = TEST_SECRET
    yield
    settings.scheduler_secret = original


@pytest.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    await db_session.execute(delete(EarningsTrade))
    await db_session.execute(delete(EarningsEvent))
    await db_session.commit()
    yield
    await db_session.execute(delete(EarningsTrade))
    await db_session.execute(delete(EarningsEvent))
    await db_session.commit()


# ---------------------------------------------------------------------------
# 1. Simple drift: moved date updates existing future row in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_updates_future_row_in_place(
    client: AsyncClient, db_session: AsyncSession
):
    """When Finnhub moves a report date, the existing future row is updated;
    a new row is NOT inserted, so the count stays at 1."""
    old_date = TODAY + timedelta(days=11)
    new_date = TODAY + timedelta(days=12)

    event_id = await _insert_event(db_session, "AAPL", old_date, fiscal_period="Q3 2026")

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    count = await _event_count(db_session, "AAPL")
    assert count == 1, f"Expected 1 row, found {count}"

    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date


@pytest.mark.asyncio
async def test_drift_preserves_event_id(
    client: AsyncClient, db_session: AsyncSession
):
    """The drifted row keeps the same UUID — trade FKs remain valid."""
    old_date = TODAY + timedelta(days=11)
    new_date = TODAY + timedelta(days=12)

    event_id = await _insert_event(db_session, "AAPL", old_date, fiscal_period="Q3 2026")
    trade_id = await _insert_trade(db_session, event_id)

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        await client.post("/api/internal/earnings/refresh-calendar", headers=auth_headers())
    finally:
        _clear_market()

    db_session.expire_all()

    # The row should still have the same id.
    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date

    # The trade still points at the (now-moved) event.
    trade = await _fetch_trade(db_session, trade_id)
    assert trade is not None
    assert trade.earnings_event_id == event_id


# ---------------------------------------------------------------------------
# 2. Drift onto an existing duplicate (already-duplicated pair)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_merges_existing_duplicate(
    client: AsyncClient, db_session: AsyncSession
):
    """If both old and new date rows already exist, the incoming event causes
    a merge: one row survives, the other is deleted, trade FKs are repointed."""
    old_date = TODAY + timedelta(days=11)
    new_date = TODAY + timedelta(days=12)

    # Existing rows (the duplicate pair the bug created).
    old_event_id = await _insert_event(
        db_session, "AAPL", old_date, fiscal_period="Q3 2026"
    )
    # Attach a trade to the old event (the one the FK must survive on).
    trade_id = await _insert_trade(db_session, old_event_id)

    # The "new" row at the target date — collision row.
    _new_event_id = await _insert_event(
        db_session, "AAPL", new_date, fiscal_period="Q3 2026"
    )

    assert await _event_count(db_session, "AAPL") == 2

    # Refresh brings in the event at new_date.
    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()

    # Exactly one row should remain.
    count = await _event_count(db_session, "AAPL")
    assert count == 1, f"Expected 1 row after merge, found {count}"

    # The trade must still exist and point at the surviving row.
    trade = await _fetch_trade(db_session, trade_id)
    assert trade is not None

    surviving = (
        await db_session.execute(
            select(EarningsEvent).where(EarningsEvent.ticker == "AAPL")
        )
    ).scalars().first()
    assert surviving is not None
    assert trade.earnings_event_id == surviving.id


# ---------------------------------------------------------------------------
# 3. Past-event immutability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_event_not_modified_by_drift(
    client: AsyncClient, db_session: AsyncSession
):
    """A past event (report_date < today) must never be modified, even when
    Finnhub returns a new date for the same fiscal_period."""
    past_date = TODAY - timedelta(days=40)
    new_date = TODAY + timedelta(days=16)

    event_id = await _insert_event(
        db_session, "AAPL", past_date, fiscal_period="Q2 2026"
    )

    # Finnhub now claims Q2 2026 is on new_date (drift on a past event).
    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=2)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()

    # Past row untouched.
    past_event = await _fetch_event(db_session, event_id)
    assert past_event is not None
    assert past_event.report_date == past_date

    # A new future row may have been inserted (that's fine — two rows now,
    # but the past one is intact).
    count = await _event_count(db_session, "AAPL")
    assert count >= 1
    assert past_event.report_date == past_date


@pytest.mark.asyncio
async def test_drift_moves_unrealized_past_row(
    client: AsyncClient, db_session: AsyncSession
):
    """Q3 row dated yesterday; Finnhub now says Q3 is next week. The stale
    row never reported (no realized move), so it is moved in place rather
    than leaving a phantom past Q3 row next to a fresh future one."""
    old_date = TODAY - timedelta(days=1)
    new_date = TODAY + timedelta(days=6)

    event_id = await _insert_event(db_session, "AAPL", old_date, fiscal_period="Q3 2026")
    trade_id = await _insert_trade(db_session, event_id)

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    assert await _event_count(db_session, "AAPL") == 1

    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date

    trade = await _fetch_trade(db_session, trade_id)
    assert trade is not None
    assert trade.earnings_event_id == event_id


@pytest.mark.asyncio
async def test_drift_moves_unrealized_past_row_null_fiscal_period(
    client: AsyncClient, db_session: AsyncSession
):
    """Same as above with no fiscal_period on either side: the ±14-day
    window plus the missing realized move identify the slipped event."""
    old_date = TODAY - timedelta(days=3)
    new_date = TODAY + timedelta(days=4)

    event_id = await _insert_event(db_session, "MSFT", old_date, fiscal_period=None)

    _set_market(_FakeMarket([_raw_event("MSFT", new_date)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    assert await _event_count(db_session, "MSFT") == 1

    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date


@pytest.mark.asyncio
async def test_backfill_then_refresh_still_moves_estimated_past_row(
    client: AsyncClient, db_session: AsyncSession
):
    """#106: Q3 estimated for 3 days ago, then the realized-move backfill
    runs before Finnhub corrects the date. The backfill must not measure
    (and so lock) the phantom row, or the correction inserts a second Q3
    row next to a phantom with a made-up move."""
    old_date = TODAY - timedelta(days=3)
    new_date = TODAY + timedelta(days=4)

    event_id = await _insert_event(db_session, "AAPL", old_date, fiscal_period="Q3 2026")

    # _FakeMarket.history raises: the backfill must not even fetch bars.
    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        backfill = await client.post(
            "/api/internal/earnings/backfill-realized-moves", headers=auth_headers()
        )
        refresh = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert backfill.status_code == 200
    assert backfill.json()["total_events"] == 0
    assert refresh.status_code == 200

    db_session.expire_all()
    assert await _event_count(db_session, "AAPL") == 1
    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date
    assert event.realized_move_pct is None


@pytest.mark.asyncio
async def test_drift_leaves_realized_past_row_alone(
    client: AsyncClient, db_session: AsyncSession
):
    """A past row that already has a realized move is a reported quarter:
    it is never moved, and the incoming event gets its own row."""
    from decimal import Decimal

    old_date = TODAY - timedelta(days=1)
    new_date = TODAY + timedelta(days=6)

    event_id = await _insert_event(
        db_session,
        "AAPL",
        old_date,
        fiscal_period="Q3 2026",
        realized_move_pct=Decimal("0.05"),
    )

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    assert await _event_count(db_session, "AAPL") == 2

    past_event = await _fetch_event(db_session, event_id)
    assert past_event is not None
    assert past_event.report_date == old_date
    assert past_event.realized_move_pct == Decimal("0.05")


@pytest.mark.asyncio
async def test_drift_leaves_unrealized_past_row_of_other_quarter_alone(
    client: AsyncClient, db_session: AsyncSession
):
    """An unrealized past row inside the window but for a different
    fiscal_period is a different event (e.g. the backfill hasn't run yet):
    it must not be merged into the incoming quarter."""
    old_date = TODAY - timedelta(days=2)
    new_date = TODAY + timedelta(days=5)

    event_id = await _insert_event(db_session, "AAPL", old_date, fiscal_period="Q2 2026")

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    assert await _event_count(db_session, "AAPL") == 2

    past_event = await _fetch_event(db_session, event_id)
    assert past_event is not None
    assert past_event.report_date == old_date
    assert past_event.fiscal_period == "Q2 2026"


# ---------------------------------------------------------------------------
# 4. Null fiscal_period falls back to ±14-day window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_null_fiscal_period_uses_date_window(
    client: AsyncClient, db_session: AsyncSession
):
    """When fiscal_period is null on both sides, drift is matched by
    the ±14-day date proximity window."""
    old_date = TODAY + timedelta(days=11)
    new_date = old_date + timedelta(days=5)  # within ±14 days

    event_id = await _insert_event(
        db_session, "MSFT", old_date, fiscal_period=None
    )

    # Incoming event has no year/quarter → fiscal_period stays null.
    _set_market(_FakeMarket([_raw_event("MSFT", new_date)]))  # no year/quarter
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    count = await _event_count(db_session, "MSFT")
    assert count == 1, f"Expected 1 row, found {count}"

    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.report_date == new_date


@pytest.mark.asyncio
async def test_drift_outside_window_inserts_new_row(
    client: AsyncClient, db_session: AsyncSession
):
    """When fiscal_period is null and the dates are more than 14 days apart,
    no drift match is made and a new row is inserted."""
    old_date = TODAY
    new_date = old_date + timedelta(days=20)  # beyond ±14-day window

    await _insert_event(db_session, "GOOG", old_date, fiscal_period=None)

    _set_market(_FakeMarket([_raw_event("GOOG", new_date)]))
    try:
        resp = await client.post(
            "/api/internal/earnings/refresh-calendar", headers=auth_headers()
        )
    finally:
        _clear_market()

    assert resp.status_code == 200

    db_session.expire_all()
    count = await _event_count(db_session, "GOOG")
    assert count == 2


# ---------------------------------------------------------------------------
# 5. Realized-move nulled on drift (edge-case safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_nulls_stale_realized_move_pct(
    client: AsyncClient, db_session: AsyncSession
):
    """If a future row somehow has realized_move_pct set and its date drifts,
    the stale value is nulled so the backfill recomputes it."""
    from decimal import Decimal

    old_date = TODAY + timedelta(days=11)
    new_date = TODAY + timedelta(days=12)

    event_id = await _insert_event(
        db_session,
        "AAPL",
        old_date,
        fiscal_period="Q3 2026",
        realized_move_pct=Decimal("0.04"),
    )

    _set_market(_FakeMarket([_raw_event("AAPL", new_date, year=2026, quarter=3)]))
    try:
        await client.post("/api/internal/earnings/refresh-calendar", headers=auth_headers())
    finally:
        _clear_market()

    db_session.expire_all()

    event = await _fetch_event(db_session, event_id)
    assert event is not None
    assert event.realized_move_pct is None


# ---------------------------------------------------------------------------
# 6. merge-duplicate-events endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_endpoint_requires_auth(client: AsyncClient):
    resp = await client.post("/api/internal/earnings/merge-duplicate-events")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_merge_endpoint_no_op_on_clean_table(
    client: AsyncClient, db_session: AsyncSession
):
    """No duplicates → zero merges, idempotent."""
    await _insert_event(
        db_session, "AAPL", TODAY + timedelta(days=11), fiscal_period="Q3 2026"
    )

    resp = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["merges"] == 0
    assert data["trades_repointed"] == 0

    # Second call also a no-op.
    resp2 = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp2.status_code == 200
    assert resp2.json()["merges"] == 0


@pytest.mark.asyncio
async def test_merge_endpoint_merges_duplicate_pair(
    client: AsyncClient, db_session: AsyncSession
):
    """Two rows for the same ticker + fiscal_period are merged to one."""
    date_a = TODAY + timedelta(days=11)
    date_b = TODAY + timedelta(days=12)

    id_a = await _insert_event(db_session, "AAPL", date_a, fiscal_period="Q3 2026")
    _id_b = await _insert_event(db_session, "AAPL", date_b, fiscal_period="Q3 2026")

    assert await _event_count(db_session, "AAPL") == 2

    resp = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["merges"] == 1

    db_session.expire_all()
    assert await _event_count(db_session, "AAPL") == 1


@pytest.mark.asyncio
async def test_merge_endpoint_repoints_trade_fks(
    client: AsyncClient, db_session: AsyncSession
):
    """After a merge, trades linked to the losing row are repointed to the keeper."""
    date_a = TODAY + timedelta(days=11)
    date_b = TODAY + timedelta(days=12)

    id_a = await _insert_event(db_session, "AAPL", date_a, fiscal_period="Q3 2026")
    id_b = await _insert_event(db_session, "AAPL", date_b, fiscal_period="Q3 2026")

    # Trades on both rows — a trade-bearing row can never be the loser when
    # the other row has none, so give each a trade; whichever loses, its
    # trade must be repointed.
    trade_a = await _insert_trade(db_session, id_a)
    trade_b = await _insert_trade(db_session, id_b)

    resp = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp.status_code == 200
    assert resp.json()["trades_repointed"] == 1

    db_session.expire_all()

    # Exactly one event row survives.
    assert await _event_count(db_session, "AAPL") == 1

    surviving = (
        await db_session.execute(
            select(EarningsEvent).where(EarningsEvent.ticker == "AAPL")
        )
    ).scalars().first()
    assert surviving is not None

    # Both trades still exist and point at the surviving event.
    for trade_id in (trade_a, trade_b):
        trade = await _fetch_trade(db_session, trade_id)
        assert trade is not None
        assert trade.earnings_event_id == surviving.id


@pytest.mark.asyncio
async def test_merge_endpoint_merges_past_duplicate_pair(
    client: AsyncClient, db_session: AsyncSession
):
    """The pairs this endpoint exists to clean (IR 07-29/30, ROL 07-21/29)
    are fully in the past by now — a past duplicate pair must still merge,
    keeping the row with realized data and counting in past_pairs_merged."""
    from decimal import Decimal

    date_a = TODAY - timedelta(days=47)
    date_b = TODAY - timedelta(days=46)

    id_a = await _insert_event(db_session, "IR", date_a, fiscal_period="Q2 2026")
    id_b = await _insert_event(
        db_session,
        "IR",
        date_b,
        fiscal_period="Q2 2026",
        realized_move_pct=Decimal("0.031"),
    )

    resp = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["merges"] == 1
    assert data["past_pairs_merged"] == 1

    db_session.expire_all()
    assert await _event_count(db_session, "IR") == 1

    # The row carrying realized data survives; the bare duplicate is gone.
    assert await _fetch_event(db_session, id_b) is not None
    assert await _fetch_event(db_session, id_a) is None


@pytest.mark.asyncio
async def test_merge_endpoint_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    """Running merge-duplicate-events twice is a no-op on the second call."""
    date_a = TODAY + timedelta(days=11)
    date_b = TODAY + timedelta(days=12)

    await _insert_event(db_session, "AAPL", date_a, fiscal_period="Q3 2026")
    await _insert_event(db_session, "AAPL", date_b, fiscal_period="Q3 2026")

    resp1 = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp1.json()["merges"] == 1

    resp2 = await client.post(
        "/api/internal/earnings/merge-duplicate-events", headers=auth_headers()
    )
    assert resp2.json()["merges"] == 0
    assert await _event_count(db_session, "AAPL") == 1
