from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.journal import JournalEntry
from app.models.trade_campaigns import TradeCampaign
from app.models.watchlist import WatchlistItem


@pytest_asyncio.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    for model in (JournalEntry, Alert, TradeCampaign, WatchlistItem):
        await db_session.execute(delete(model))
    await db_session.commit()
    yield
    for model in (JournalEntry, Alert, TradeCampaign, WatchlistItem):
        await db_session.execute(delete(model))
    await db_session.commit()


@pytest.mark.asyncio
async def test_summary_counts_owned_as_open_positions(
    client: AsyncClient, db_session: AsyncSession
):
    """The 'owned' status (vault-sync era) counts as an open position."""
    for ticker, status in [
        ("AXON", "owned"),
        ("RBLX", "owned"),
        ("AAPL", "watching"),
        ("FICO", "closed"),
    ]:
        db_session.add(WatchlistItem(ticker=ticker, status=status))
    await db_session.commit()

    response = await client.get("/api/dashboard/summary")
    assert response.status_code == 200
    watchlist = response.json()["watchlist"]
    assert watchlist == {"watching": 1, "open": 2, "closed": 1, "total": 4}


@pytest.mark.asyncio
async def test_summary_recent_activity_includes_vault_entries(
    client: AsyncClient, db_session: AsyncSession
):
    """Recent activity is the merged journal feed, not just journal_entries."""
    db_session.add(
        TradeCampaign(
            title="FICO-2026-07",
            vault_path="Closed/FICO-2026-07.md",
            ticker="FICO",
            status="closed",
            body="## Thesis\n\nOversold bounce.\n\n## Timeline\n\n### 2026-07-14 — Bought\n- filled\n",
            content_hash="x" * 64,
            opened_on=date(2026, 7, 14),
            synced_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    response = await client.get("/api/dashboard/summary")
    assert response.status_code == 200
    recent = response.json()["recent_journal"]
    assert len(recent) == 2
    assert all(e["source"] == "vault" for e in recent)
    assert all(e["ticker"] == "FICO" for e in recent)
