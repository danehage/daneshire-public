"""Vault sync tests (#61): frontmatter parsing, upsert semantics, the
watchlist side effect that feeds the #57 plateau grades, and the sync route.

Campaign fixtures are synthetic but mirror the real DaneBrain format exactly
(frontmatter keys, hand-typed comma numbers, empty closed:, projected
sections).
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.models.trade_campaigns import TradeCampaign
from app.models.watchlist import WatchlistItem
from app.schemas.trade_campaigns import VaultSyncFile
from app.services.vault_sync import VaultSyncService, split_frontmatter

OPEN_CAMPAIGN = """---
type: trade
ticker: UPS
strategy: long_shares
status: open
opened: 2026-04-10
closed:
thesis_tags: [dividend-income, industrial]
cost_basis: 100.935
realized_pnl:
max_risk: 40,374.00
account: roth
---

## Thesis

**"The Automation Arbitrage."**

| Scenario | Target | Logic |
|---|---|---|
| Best Case | $135.00 | Margin expansion. |
"""

CLOSED_CAMPAIGN = """---
type: trade
ticker: AJG
strategy: long_shares
status: closed
opened: 2026-05-21
closed: 2026-06-26
thesis_tags: [insurance-brokerage]
cost_basis: 207.81
realized_pnl: 2,458.01
max_risk: 29,925
account: traditional_ira
---

## Timeline

### 2026-05-21 — Bought shares
- Bought 144 shares @ $207.81
"""

BROKEN_CAMPAIGN = """---
ticker: [unclosed
---

body after broken frontmatter
"""


@pytest.fixture(autouse=True)
async def clean_tables(db_session):
    await db_session.execute(delete(TradeCampaign))
    await db_session.execute(delete(WatchlistItem))
    await db_session.commit()
    yield
    await db_session.execute(delete(TradeCampaign))
    await db_session.execute(delete(WatchlistItem))
    await db_session.commit()


def _files(*pairs):
    return [VaultSyncFile(path=p, content=c) for p, c in pairs]


# --- split_frontmatter -------------------------------------------------------


def test_split_frontmatter_valid():
    fm, body, error = split_frontmatter(OPEN_CAMPAIGN)
    assert error is None
    assert fm["ticker"] == "UPS"
    assert fm["closed"] is None
    assert body.startswith("\n## Thesis")


def test_split_frontmatter_missing_block():
    fm, body, error = split_frontmatter("# just markdown\n")
    assert fm == {} and error == "no frontmatter block"
    assert body == "# just markdown\n"


def test_split_frontmatter_unterminated():
    fm, body, error = split_frontmatter("---\nticker: UPS\n")
    assert error == "unterminated frontmatter block"


# --- sync semantics ----------------------------------------------------------


async def test_sync_creates_campaigns_and_parses_frontmatter(db_session):
    service = VaultSyncService(db_session)
    result = await service.sync(
        _files(
            ("Open/UPS-2026-04.md", OPEN_CAMPAIGN),
            ("Closed/AJG-2026-05.md", CLOSED_CAMPAIGN),
        )
    )
    assert sorted(result.upserted) == [
        "Closed/AJG-2026-05.md",
        "Open/UPS-2026-04.md",
    ]
    assert result.errors == {}

    rows = {
        r.title: r
        for r in (await db_session.execute(select(TradeCampaign))).scalars()
    }
    ups = rows["UPS-2026-04"]
    assert ups.status == "open"
    assert ups.ticker == "UPS"
    assert ups.opened_on == date(2026, 4, 10)
    assert ups.closed_on is None
    assert ups.cost_basis == Decimal("100.935")
    assert ups.account == "roth"
    assert "Automation Arbitrage" in ups.body

    ajg = rows["AJG-2026-05"]
    assert ajg.status == "closed"
    # Hand-typed comma numbers must parse.
    assert ajg.realized_pnl == Decimal("2458.01")
    assert ajg.frontmatter["max_risk"] == "29,925"


async def test_sync_is_idempotent(db_session):
    service = VaultSyncService(db_session)
    files = _files(("Open/UPS-2026-04.md", OPEN_CAMPAIGN))
    first = await service.sync(files)
    assert len(first.upserted) == 1
    second = await service.sync(files)
    assert second.upserted == [] and second.unchanged == 1


async def test_open_to_closed_move_flips_status_without_duplicate(db_session):
    service = VaultSyncService(db_session)
    await service.sync(_files(("Open/UPS-2026-04.md", OPEN_CAMPAIGN)))
    row_id = (
        (await db_session.execute(select(TradeCampaign))).scalars().one().id
    )

    closed_content = OPEN_CAMPAIGN.replace("status: open", "status: closed").replace(
        "closed:\n", "closed: 2026-08-01\n"
    )
    await service.sync(_files(("Closed/UPS-2026-04.md", closed_content)))

    rows = list((await db_session.execute(select(TradeCampaign))).scalars())
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].status == "closed"
    assert rows[0].closed_on == date(2026, 8, 1)
    assert rows[0].vault_path == "Closed/UPS-2026-04.md"


async def test_mid_move_duplicate_closed_copy_wins(db_session):
    """File present in BOTH folders (caught mid-move): the Closed/ copy wins."""
    service = VaultSyncService(db_session)
    closed_content = OPEN_CAMPAIGN.replace("status: open", "status: closed")
    await service.sync(
        _files(
            ("Closed/UPS-2026-04.md", closed_content),
            ("Open/UPS-2026-04.md", OPEN_CAMPAIGN),
        )
    )
    rows = list((await db_session.execute(select(TradeCampaign))).scalars())
    assert len(rows) == 1
    assert rows[0].status == "closed"


async def test_missing_file_soft_archives(db_session):
    service = VaultSyncService(db_session)
    await service.sync(
        _files(
            ("Open/UPS-2026-04.md", OPEN_CAMPAIGN),
            ("Closed/AJG-2026-05.md", CLOSED_CAMPAIGN),
        )
    )
    result = await service.sync(_files(("Closed/AJG-2026-05.md", CLOSED_CAMPAIGN)))
    assert result.archived == ["UPS-2026-04"]
    ups = (
        await db_session.execute(
            select(TradeCampaign).where(TradeCampaign.title == "UPS-2026-04")
        )
    ).scalars().one()
    assert ups.status == "archived"


async def test_parse_error_surfaced_but_file_stored(db_session):
    service = VaultSyncService(db_session)
    result = await service.sync(_files(("Open/RBLX-2026-04.md", BROKEN_CAMPAIGN)))
    assert "Open/RBLX-2026-04.md" in result.errors
    row = (await db_session.execute(select(TradeCampaign))).scalars().one()
    assert row.title == "RBLX-2026-04"
    assert row.ticker == "RBLX"  # falls back to the filename stem
    assert row.parse_error is not None


async def test_unknown_folder_rejected_per_file(db_session):
    service = VaultSyncService(db_session)
    result = await service.sync(
        _files(
            ("Templates/trade-template.md", OPEN_CAMPAIGN),
            ("Open/UPS-2026-04.md", OPEN_CAMPAIGN),
        )
    )
    assert "Templates/trade-template.md" in result.errors
    assert result.upserted == ["Open/UPS-2026-04.md"]


# --- watchlist side effect (#57 gap-gauge dependency) ------------------------


async def test_open_campaign_creates_owned_watchlist_item(db_session):
    service = VaultSyncService(db_session)
    result = await service.sync(_files(("Open/UPS-2026-04.md", OPEN_CAMPAIGN)))
    assert result.watchlist_updated == ["UPS"]
    item = (await db_session.execute(select(WatchlistItem))).scalars().one()
    assert item.ticker == "UPS"
    assert item.status == "owned"
    assert item.entry_date == date(2026, 4, 10)
    assert item.entry_price == Decimal("100.935")
    assert item.position_type == "long"


async def test_open_campaign_promotes_existing_watchlist_item(db_session):
    db_session.add(WatchlistItem(ticker="UPS", status="watching"))
    await db_session.commit()
    service = VaultSyncService(db_session)
    await service.sync(_files(("Open/UPS-2026-04.md", OPEN_CAMPAIGN)))
    item = (await db_session.execute(select(WatchlistItem))).scalars().one()
    assert item.status == "owned"
    assert item.entry_date == date(2026, 4, 10)


async def test_closed_campaign_leaves_watchlist_alone(db_session):
    service = VaultSyncService(db_session)
    result = await service.sync(_files(("Closed/AJG-2026-05.md", CLOSED_CAMPAIGN)))
    assert result.watchlist_updated == []
    items = list((await db_session.execute(select(WatchlistItem))).scalars())
    assert items == []


# --- routes -------------------------------------------------------------------


async def test_sync_route_and_list_and_detail(client):
    response = await client.post(
        "/api/trade-campaigns/sync",
        json={
            "files": [
                {"path": "Open/UPS-2026-04.md", "content": OPEN_CAMPAIGN},
                {"path": "Closed/AJG-2026-05.md", "content": CLOSED_CAMPAIGN},
            ]
        },
    )
    assert response.status_code == 200
    assert len(response.json()["upserted"]) == 2

    listing = await client.get("/api/trade-campaigns")
    assert listing.status_code == 200
    campaigns = listing.json()
    assert [c["status"] for c in campaigns] == ["open", "closed"]
    assert "body" not in campaigns[0]

    detail = await client.get(f"/api/trade-campaigns/{campaigns[0]['id']}")
    assert detail.status_code == 200
    assert "Automation Arbitrage" in detail.json()["body"]


async def test_sync_route_rejects_empty_file_list(client):
    response = await client.post("/api/trade-campaigns/sync", json={"files": []})
    assert response.status_code == 422


async def test_open_campaign_owns_active_row_not_older_closed_row(db_session):
    """With a closed row from an earlier position and a newer watching row,
    the sync promotes the watching row. Oldest-first reopened the closed
    row beside it, leaving two active rows for one ticker."""
    closed = WatchlistItem(
        ticker="UPS", status="closed",
        created_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    watching = WatchlistItem(
        ticker="UPS", status="watching",
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([closed, watching])
    await db_session.commit()

    await VaultSyncService(db_session).sync(
        _files(("Open/UPS-2026-04.md", OPEN_CAMPAIGN))
    )

    db_session.expire_all()
    rows = {
        r.id: r.status
        for r in (
            await db_session.execute(
                select(WatchlistItem).where(WatchlistItem.ticker == "UPS")
            )
        ).scalars()
    }
    assert rows == {closed.id: "closed", watching.id: "owned"}
