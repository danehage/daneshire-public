"""Unit tests for the vault campaign body -> journal feed projection."""

from datetime import date, datetime, timezone
from uuid import uuid4

from app.models.trade_campaigns import TradeCampaign
from app.services.campaign_journal import parse_campaign_entries


def _campaign(body: str, **overrides) -> TradeCampaign:
    fields = dict(
        id=uuid4(),
        title="AXON-2026-04",
        vault_path="Open/AXON-2026-04.md",
        ticker="AXON",
        status="open",
        body=body,
        content_hash="x" * 64,
        opened_on=date(2026, 4, 15),
        closed_on=None,
        synced_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return TradeCampaign(**fields)


def test_full_body_projection():
    body = (
        "## Thesis\n\nThe monopoly reset.\n\n"
        "## Timeline\n\n"
        "### 2026-04-15 — Position Established\n- Bought 100 @ $399.62\n\n"
        "### 2026-05-20 — Sold covered call\n- Sold 1x $480C\n\n"
        "## P&L Summary\n\n| not | journal |\n\n"
        "## Exit / Review\n\nCalled away at target.\n"
    )
    campaign = _campaign(body, closed_on=date(2026, 9, 18))
    entries = parse_campaign_entries(campaign)

    assert [e.entry_type for e in entries] == ["thesis", "note", "note", "review"]
    assert all(e.source == "vault" for e in entries)
    assert all(e.ticker == "AXON" for e in entries)
    assert all(e.campaign_id == campaign.id for e in entries)

    thesis, buy, call, review = entries
    assert thesis.content == "The monopoly reset."
    assert thesis.title == "AXON-2026-04"
    assert thesis.created_at.date() == date(2026, 4, 15)  # opened_on
    assert buy.title == "Position Established"
    assert buy.created_at.date() == date(2026, 4, 15)
    assert call.title == "Sold covered call"
    assert call.created_at.date() == date(2026, 5, 20)
    assert review.created_at.date() == date(2026, 9, 18)  # closed_on
    assert buy.id == f"vault:{campaign.id}:timeline:0"


def test_empty_review_section_skipped():
    body = "## Timeline\n\n### 2026-04-15 — Entry\n- filled\n\n## Exit / Review\n\n"
    entries = parse_campaign_entries(_campaign(body))
    assert [e.entry_type for e in entries] == ["note"]


def test_placeholder_date_falls_back_to_opened_on():
    body = "## Timeline\n\n### 2026-05-XX — Earnings (Projected)\n- watch margins\n"
    entries = parse_campaign_entries(_campaign(body))
    assert len(entries) == 1
    assert entries[0].title == "Earnings (Projected)"
    assert entries[0].created_at.date() == date(2026, 4, 15)


def test_undated_heading_keeps_full_title():
    body = "## Timeline\n\n### Sold the call\n- premium collected\n"
    entries = parse_campaign_entries(_campaign(body))
    assert entries[0].title == "Sold the call"
    assert entries[0].created_at.date() == date(2026, 4, 15)


def test_hyphen_separator_and_no_title():
    body = "## Timeline\n\n### 2026-06-01 - Snapshot\n- marks\n\n### 2026-06-02\n- bare date\n"
    entries = parse_campaign_entries(_campaign(body))
    assert entries[0].title == "Snapshot"
    assert entries[1].title is None
    assert entries[1].created_at.date() == date(2026, 6, 2)


def test_no_opened_on_falls_back_to_synced_at():
    body = "## Thesis\n\nSome thesis.\n"
    entries = parse_campaign_entries(_campaign(body, opened_on=None))
    assert entries[0].created_at.date() == date(2026, 8, 1)


def test_body_without_journal_sections_yields_nothing():
    body = "# Freeform\n\nJust prose, no recognized sections.\n"
    assert parse_campaign_entries(_campaign(body)) == []
