"""
Tests for ``POST /api/internal/earnings/refresh-snapshots``.

Verify the HTTP surface (auth, summary shape, no-data + rejected-move
buckets) and the IV-rank cutover threshold (ADR-0004) at the 251 / 252 /
253 boundary.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.earnings import EarningsEvent
from app.models.iv_snapshots import IVSnapshot
from app.models.watchlist import WatchlistItem
from app.schemas.iv import IVSnapshotRaw
from app.schemas.market import PriceQuote
from app.services.market import OptionsDataUnavailable, UpstreamError, get_market


TEST_SECRET = "test-scheduler-secret"


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
    """Wipe iv_snapshots + watchlist_items + earnings_events around each test."""
    for model in (IVSnapshot, WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()
    yield
    for model in (IVSnapshot, WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()


@pytest.fixture(autouse=True)
async def pin_gate_thresholds():
    """Pin the Stage-1 gate thresholds so tests don't drift with defaults."""
    original = (
        settings.earnings_gate_min_price,
        settings.earnings_gate_min_avg_volume,
        settings.earnings_gate_min_market_cap,
    )
    settings.earnings_gate_min_price = 20.0
    settings.earnings_gate_min_avg_volume = 1_000_000
    settings.earnings_gate_min_market_cap = 2_000_000_000
    yield
    (
        settings.earnings_gate_min_price,
        settings.earnings_gate_min_avg_volume,
        settings.earnings_gate_min_market_cap,
    ) = original


def auth_headers():
    return {"X-Scheduler-Secret": TEST_SECRET}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeMarket:
    """Returns canned :class:`IVSnapshotRaw` results per ticker.

    Tickers whose responses are :class:`Exception` instances are
    surfaced through the seam contract as per-ticker errors in the
    result dict.
    """

    def __init__(self, responses: dict, quotes: dict | None = None):
        self.responses = responses
        self.quote_responses = quotes or {}
        self.snapshot_calls: list[list[str]] = []

    async def iv_snapshots(self, tickers):
        self.snapshot_calls.append(list(tickers))
        out = {}
        for t in tickers:
            t = t.upper()
            value = self.responses.get(t)
            if value is None:
                out[t] = OptionsDataUnavailable(t, "missing in fake")
            elif isinstance(value, Exception):
                out[t] = value
            else:
                out[t] = value
        return out

    async def quotes(self, tickers):
        out = {}
        for t in tickers:
            t = t.upper()
            value = self.quote_responses.get(t)
            if value is None:
                out[t] = UpstreamError(t, "no quote in fake")
            else:
                out[t] = value
        return out


def _override_market(responses: dict, quotes: dict | None = None):
    fake = _FakeMarket(responses, quotes)
    app.dependency_overrides[get_market] = lambda: fake
    return fake


async def _add_watchlist(db: AsyncSession, ticker: str, tags=("earnings-candidate",)):
    item = WatchlistItem(ticker=ticker.upper(), tags=list(tags))
    db.add(item)
    await db.commit()


async def _add_event(db: AsyncSession, ticker: str, days_out: int = 3):
    db.add(
        EarningsEvent(
            ticker=ticker.upper(),
            report_date=date.today() + timedelta(days=days_out),
            report_time="amc",
        )
    )
    await db.commit()


def _quote(
    ticker: str,
    *,
    price: float = 100.0,
    volume: int = 3_000_000,
    avg_volume: int = 5_000_000,
    market_cap: int = 10_000_000_000,
) -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price=price,
        volume=volume,
        avg_volume=avg_volume,
        market_cap=market_cap,
    )


def _snapshot(ticker: str, *, iv30="0.30", rank="50", em="0.04") -> IVSnapshotRaw:
    return IVSnapshotRaw(
        ticker=ticker,
        iv30=Decimal(iv30),
        iv_rank_provider=Decimal(rank),
        expected_move_pct=Decimal(em),
    )


async def _seed_history(db: AsyncSession, ticker: str, rows: int):
    """Insert `rows` historical iv_snapshots ending yesterday."""
    base = date.today() - timedelta(days=1)
    for i in range(rows):
        db.add(
            IVSnapshot(
                ticker=ticker,
                snapshot_date=base - timedelta(days=i),
                iv30=Decimal("0.20") + Decimal(i) / Decimal(10000),
                iv_rank=Decimal("50"),
                expected_move_pct=Decimal("0.03"),
                source="tastytrade",
            )
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Auth + empty watchlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_scheduler_secret(client: AsyncClient):
    response = await client.post("/api/internal/earnings/refresh-snapshots")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_secret_rejected(client: AsyncClient):
    response = await client.post(
        "/api/internal/earnings/refresh-snapshots",
        headers={"X-Scheduler-Secret": "not-the-secret"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_empty_watchlist_returns_zero(client: AsyncClient):
    _override_market({})
    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == 0
    assert body["skipped_no_data"] == []
    assert body["skipped_rejected_move"] == []


# ---------------------------------------------------------------------------
# Happy path + bucketing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_one_row_per_earnings_candidate(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_watchlist(db_session, "AAPL")
    await _add_watchlist(db_session, "MSFT")
    # Non-candidate ticker — should be ignored.
    await _add_watchlist(db_session, "GOOG", tags=("watching",))

    _override_market(
        {"AAPL": _snapshot("AAPL"), "MSFT": _snapshot("MSFT")}
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["written"] == 2
    assert body["skipped_no_data"] == []
    assert body["skipped_rejected_move"] == []

    rows = (await db_session.execute(select(IVSnapshot).order_by(IVSnapshot.ticker))).scalars().all()
    assert [r.ticker for r in rows] == ["AAPL", "MSFT"]
    # Source defaults to 'tastytrade' below the 252 cutover.
    assert all(r.source == "tastytrade" for r in rows)


@pytest.mark.asyncio
async def test_skips_tickers_with_no_data(client: AsyncClient, db_session: AsyncSession):
    await _add_watchlist(db_session, "AAPL")
    await _add_watchlist(db_session, "BUST")

    _override_market(
        {
            "AAPL": _snapshot("AAPL"),
            "BUST": OptionsDataUnavailable("BUST", "no chain today"),
        }
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["written"] == 1
    assert body["skipped_no_data"] == ["BUST"]
    assert body["skipped_rejected_move"] == []
    assert body["skip_reasons"] == {"no_data": 1}


@pytest.mark.asyncio
async def test_skip_reasons_bucket_causes(client: AsyncClient, db_session: AsyncSession):
    """#77: the summary counts why each ticker was skipped, so an
    after-hours stale-quote wave is visible from one log line."""
    for t in ("AAPL", "ILLQ", "THRT", "GONE", "NOEM"):
        await _add_watchlist(db_session, t)

    _override_market(
        {
            "AAPL": _snapshot("AAPL"),
            "ILLQ": OptionsDataUnavailable("ILLQ", "stale straddle", reason="stale_quote"),
            "THRT": OptionsDataUnavailable("THRT", "429", reason="rate_limited"),
            "GONE": UpstreamError("GONE"),
            "NOEM": IVSnapshotRaw(
                ticker="NOEM",
                iv30=Decimal("0.40"),
                iv_rank_provider=Decimal("70"),
                expected_move_pct=None,
            ),
        }
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["written"] == 1
    assert body["skip_reasons"] == {
        "stale_quote": 1,
        "rate_limited": 1,
        "error": 1,
        "rejected_move": 1,
    }


@pytest.mark.asyncio
async def test_skips_tickers_with_rejected_move(client: AsyncClient, db_session: AsyncSession):
    await _add_watchlist(db_session, "AAPL")
    await _add_watchlist(db_session, "STALE")

    _override_market(
        {
            "AAPL": _snapshot("AAPL"),
            "STALE": IVSnapshotRaw(
                ticker="STALE",
                iv30=Decimal("0.40"),
                iv_rank_provider=Decimal("70"),
                expected_move_pct=None,  # ADR-0003 rejection
            ),
        }
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["written"] == 1
    assert body["skipped_no_data"] == []
    assert body["skipped_rejected_move"] == ["STALE"]


@pytest.mark.asyncio
async def test_same_day_rerun_is_idempotent(client: AsyncClient, db_session: AsyncSession):
    await _add_watchlist(db_session, "AAPL")
    _override_market({"AAPL": _snapshot("AAPL")})

    first = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    second = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert first.status_code == 200
    assert second.status_code == 200

    rows = (await db_session.execute(select(IVSnapshot))).scalars().all()
    assert len(rows) == 1  # unique (ticker, snapshot_date) prevents duplicates


# ---------------------------------------------------------------------------
# IV-rank cutover threshold (ADR-0004)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iv_rank_uses_provider_below_cutover(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_watchlist(db_session, "AAPL")
    await _seed_history(db_session, "AAPL", rows=251)
    _override_market({"AAPL": _snapshot("AAPL", rank="42")})

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200

    today_row = (
        await db_session.execute(
            select(IVSnapshot).where(
                IVSnapshot.ticker == "AAPL", IVSnapshot.snapshot_date == date.today()
            )
        )
    ).scalar_one()
    assert today_row.source == "tastytrade"
    assert Decimal(today_row.iv_rank) == Decimal("42")


@pytest.mark.asyncio
async def test_iv_rank_uses_self_computed_at_cutover(
    client: AsyncClient, db_session: AsyncSession
):
    # Seed exactly 252 prior rows → cutover triggers on this run.
    await _add_watchlist(db_session, "AAPL")
    await _seed_history(db_session, "AAPL", rows=252)
    # iv30 today (0.30) compared to seeded min/max (0.20 .. 0.20+251/10000)
    # produces a high rank well inside [0, 100].
    _override_market({"AAPL": _snapshot("AAPL", iv30="0.30", rank="42")})

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200

    today_row = (
        await db_session.execute(
            select(IVSnapshot).where(
                IVSnapshot.ticker == "AAPL", IVSnapshot.snapshot_date == date.today()
            )
        )
    ).scalar_one()
    assert today_row.source == "self_252d"
    # Provider value (42) must NOT have been used.
    assert Decimal(today_row.iv_rank) != Decimal("42")


@pytest.mark.asyncio
async def test_iv_rank_clamped_to_bounded_range(
    client: AsyncClient, db_session: AsyncSession
):
    """With ≥252 rows and today's iv30 well above the historical max,
    the self-computed rank must clamp to 100, not exceed it.
    """
    await _add_watchlist(db_session, "AAPL")
    await _seed_history(db_session, "AAPL", rows=253)
    _override_market({"AAPL": _snapshot("AAPL", iv30="9.99", rank="42")})

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200

    today_row = (
        await db_session.execute(
            select(IVSnapshot).where(
                IVSnapshot.ticker == "AAPL", IVSnapshot.snapshot_date == date.today()
            )
        )
    ).scalar_one()
    assert today_row.source == "self_252d"
    assert Decimal(today_row.iv_rank) == Decimal("100")


# ---------------------------------------------------------------------------
# Stage-1 liquidity gate (#65)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_snapshots_liquid_ticker_with_upcoming_event(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_event(db_session, "NVDA", days_out=3)

    _override_market(
        {"NVDA": _snapshot("NVDA")},
        quotes={"NVDA": _quote("NVDA")},
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated"] == 1
    assert body["passed_gate"] == 1
    assert body["failed_gate"] == 0
    assert body["written"] == 1

    rows = (await db_session.execute(select(IVSnapshot))).scalars().all()
    assert [r.ticker for r in rows] == ["NVDA"]


@pytest.mark.asyncio
async def test_gate_thresholds_are_inclusive_and_each_metric_gates(
    client: AsyncClient, db_session: AsyncSession
):
    """Exact-threshold quote passes; one ticker failing each metric fails."""
    for t in ("EXACT", "LOWPX", "LOWVOL", "LOWCAP"):
        await _add_event(db_session, t)

    quotes = {
        "EXACT": _quote(
            "EXACT", price=20.0, avg_volume=1_000_000, market_cap=2_000_000_000
        ),
        "LOWPX": _quote("LOWPX", price=19.99),
        "LOWVOL": _quote("LOWVOL", avg_volume=999_999),
        "LOWCAP": _quote("LOWCAP", market_cap=1_999_999_999),
    }
    _override_market({"EXACT": _snapshot("EXACT")}, quotes=quotes)

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["evaluated"] == 4
    assert body["passed_gate"] == 1
    assert body["failed_gate"] == 3
    assert body["written"] == 1

    rows = (await db_session.execute(select(IVSnapshot))).scalars().all()
    assert [r.ticker for r in rows] == ["EXACT"]


@pytest.mark.asyncio
async def test_gate_quote_error_counts_as_failed(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_event(db_session, "NVDA")
    await _add_event(db_session, "GHOST")

    # GHOST has no quote configured → fake returns UpstreamError.
    _override_market(
        {"NVDA": _snapshot("NVDA")},
        quotes={"NVDA": _quote("NVDA")},
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["evaluated"] == 2
    assert body["passed_gate"] == 1
    assert body["failed_gate"] == 1
    # Gate failures never reach the snapshot stage, so no skip buckets.
    assert body["skipped_no_data"] == []
    assert body["written"] == 1


@pytest.mark.asyncio
async def test_tagged_ticker_bypasses_gate(
    client: AsyncClient, db_session: AsyncSession
):
    """An illiquid tagged ticker is snapshotted without being evaluated,
    even when it also has an upcoming event."""
    await _add_watchlist(db_session, "PENNY")
    await _add_event(db_session, "PENNY", days_out=2)

    # No quotes configured at all — the gate must not need one for PENNY.
    _override_market({"PENNY": _snapshot("PENNY")})

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["evaluated"] == 0
    assert body["passed_gate"] == 0
    assert body["failed_gate"] == 0
    assert body["written"] == 1

    rows = (await db_session.execute(select(IVSnapshot))).scalars().all()
    assert [r.ticker for r in rows] == ["PENNY"]


@pytest.mark.asyncio
async def test_events_outside_horizon_not_evaluated(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_event(db_session, "LATER", days_out=20)
    await _add_event(db_session, "GONE", days_out=-5)

    _override_market({}, quotes={"LATER": _quote("LATER"), "GONE": _quote("GONE")})

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["evaluated"] == 0
    assert body["written"] == 0


@pytest.mark.asyncio
async def test_provider_iv_rank_clamped_to_bounds(
    client: AsyncClient, db_session: AsyncSession
):
    """Tastytrade's rank escapes [0, 1] when current IV breaches its own
    52-week range; the provider path must clamp like the self path or the
    insert violates `iv_rank_bounded` (prod CheckViolationError)."""
    await _add_watchlist(db_session, "HOT")
    await _add_watchlist(db_session, "COLD")
    _override_market(
        {
            "HOT": _snapshot("HOT", rank="132.5"),
            "COLD": _snapshot("COLD", rank="-4"),
        }
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200
    assert response.json()["written"] == 2

    rows = (
        await db_session.execute(select(IVSnapshot).order_by(IVSnapshot.ticker))
    ).scalars().all()
    by_ticker = {r.ticker: r for r in rows}
    assert Decimal(by_ticker["HOT"].iv_rank) == Decimal("100")
    assert Decimal(by_ticker["COLD"].iv_rank) == Decimal("0")
    assert all(r.source == "tastytrade" for r in rows)


@pytest.mark.asyncio
async def test_snapshots_fetched_and_committed_in_batches(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """The fetch/write cycle must be chunked so no transaction spans a
    long provider fetch (Neon closes idle in-transaction connections)."""
    from app.services import iv_snapshots

    monkeypatch.setattr(iv_snapshots, "SNAPSHOT_BATCH_SIZE", 2)

    for t in ("AAA", "BBB", "CCC"):
        await _add_watchlist(db_session, t)
    fake = _override_market(
        {t: _snapshot(t) for t in ("AAA", "BBB", "CCC")}
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    assert response.status_code == 200
    assert response.json()["written"] == 3

    assert fake.snapshot_calls == [["AAA", "BBB"], ["CCC"]]

    rows = (await db_session.execute(select(IVSnapshot).order_by(IVSnapshot.ticker))).scalars().all()
    assert [r.ticker for r in rows] == ["AAA", "BBB", "CCC"]


@pytest.mark.asyncio
async def test_gate_falls_back_to_todays_volume_when_avg_missing(
    client: AsyncClient, db_session: AsyncSession
):
    await _add_event(db_session, "NEWIPO")

    _override_market(
        {"NEWIPO": _snapshot("NEWIPO")},
        quotes={"NEWIPO": _quote("NEWIPO", avg_volume=0, volume=2_000_000)},
    )

    response = await client.post(
        "/api/internal/earnings/refresh-snapshots", headers=auth_headers()
    )
    body = response.json()
    assert body["passed_gate"] == 1
    assert body["written"] == 1
