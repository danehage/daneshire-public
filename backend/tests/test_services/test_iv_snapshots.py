"""Direct tests for :mod:`app.services.iv_snapshots` (#107).

The HTTP surface of ``POST /api/internal/earnings/refresh-snapshots`` is
covered in ``tests/test_routes/test_internal_iv_refresh.py``. These call the
service functions without the route: ``clamp_rank`` is pure, and the
``resolve_iv_rank`` / ``refresh_iv_snapshots`` tests use the test database.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.earnings import EarningsEvent
from app.models.iv_snapshots import IVSnapshot
from app.models.watchlist import WatchlistItem
from app.schemas.iv import IVSnapshotRaw
from app.services.iv_snapshots import (
    IV_RANK_CUTOVER_ROWS,
    clamp_rank,
    refresh_iv_snapshots,
    resolve_iv_rank,
)
from app.services.market import OptionsDataUnavailable


# ---------------------------------------------------------------------------
# clamp_rank (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rank, expected",
    [
        (Decimal("-12.5"), Decimal(0)),
        (Decimal("0"), Decimal("0")),
        (Decimal("37.25"), Decimal("37.25")),
        (Decimal("100"), Decimal("100")),
        (Decimal("143.9"), Decimal(100)),
    ],
)
def test_clamp_rank_bounds_to_0_100(rank, expected):
    assert clamp_rank(rank) == expected


# ---------------------------------------------------------------------------
# DB-backed: resolve_iv_rank + refresh_iv_snapshots
# ---------------------------------------------------------------------------


@pytest.fixture
async def clean_tables(db_session: AsyncSession):
    for model in (IVSnapshot, WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()
    yield
    for model in (IVSnapshot, WatchlistItem, EarningsEvent):
        await db_session.execute(delete(model))
    await db_session.commit()


def _raw(ticker: str, *, iv30="0.30", rank="42", em="0.04") -> IVSnapshotRaw:
    return IVSnapshotRaw(
        ticker=ticker,
        iv30=Decimal(iv30),
        iv_rank_provider=Decimal(rank),
        expected_move_pct=Decimal(em) if em is not None else None,
    )


async def _seed_history(db: AsyncSession, ticker: str, iv30s: list[str]):
    """Insert one prior row per value, newest first, ending yesterday."""
    base = date.today() - timedelta(days=1)
    for i, iv30 in enumerate(iv30s):
        db.add(
            IVSnapshot(
                ticker=ticker,
                snapshot_date=base - timedelta(days=i),
                iv30=Decimal(iv30),
                iv_rank=Decimal("50"),
                expected_move_pct=Decimal("0.03"),
                source="tastytrade",
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_resolve_uses_provider_below_cutover(db_session, clean_tables):
    await _seed_history(db_session, "AAPL", ["0.20"] * (IV_RANK_CUTOVER_ROWS - 1))

    rank, source = await resolve_iv_rank(db_session, "AAPL", _raw("AAPL", rank="42"))

    assert (rank, source) == (Decimal("42"), "tastytrade")


@pytest.mark.asyncio
async def test_resolve_self_computes_over_newest_252_rows(db_session, clean_tables):
    # Newest 252 rows span 0.20..0.40; one older row at 0.90 must be ignored.
    iv30s = ["0.40"] + ["0.30"] * (IV_RANK_CUTOVER_ROWS - 2) + ["0.20", "0.90"]
    await _seed_history(db_session, "AAPL", iv30s)

    rank, source = await resolve_iv_rank(db_session, "AAPL", _raw("AAPL", iv30="0.25"))

    assert source == "self_252d"
    assert rank == Decimal("25")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_provider_on_flat_history(db_session, clean_tables):
    await _seed_history(db_session, "AAPL", ["0.30"] * IV_RANK_CUTOVER_ROWS)

    rank, source = await resolve_iv_rank(
        db_session, "AAPL", _raw("AAPL", iv30="0.35", rank="140")
    )

    assert (rank, source) == (Decimal(100), "tastytrade")


class _FakeMarket:
    def __init__(self, snapshots: dict):
        self.snapshots = snapshots

    async def quotes(self, tickers):
        return {}

    async def iv_snapshots(self, tickers):
        return {
            t: self.snapshots.get(t) or OptionsDataUnavailable(t, "missing in fake")
            for t in tickers
        }


@pytest.mark.asyncio
async def test_refresh_writes_tagged_tickers_and_buckets_skips(db_session, clean_tables):
    for t in ("AAA", "BBB", "CCC"):
        db_session.add(WatchlistItem(ticker=t, tags=["earnings-candidate"]))
    await db_session.commit()
    market = _FakeMarket({"AAA": _raw("AAA"), "CCC": _raw("CCC", em=None)})

    summary = await refresh_iv_snapshots(db_session, market)

    assert summary.snapshot_date == date.today()
    assert summary.written == 1
    assert summary.skipped_no_data == ["BBB"]
    assert summary.skipped_rejected_move == ["CCC"]
    rows = (await db_session.execute(select(IVSnapshot))).scalars().all()
    assert [(r.ticker, r.source) for r in rows] == [("AAA", "tastytrade")]
