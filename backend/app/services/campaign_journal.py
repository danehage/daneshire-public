"""Read-time parsing of vault campaign bodies into virtual journal entries.

The DaneBrain vault is authoritative for trade journals (#61); campaign
bodies land in ``trade_campaigns.body`` as one markdown blob. This module
projects that blob into read-only entries for the merged journal feed:

- ``## Thesis``            -> one ``thesis`` entry, dated ``opened_on``
- ``## Timeline`` sections -> one ``note`` entry per ``### YYYY-MM-DD — Title``
- ``## Exit / Review``     -> one ``review`` entry, dated ``closed_on``

Other ``##`` sections (P&L summaries, etc.) are not journal entries and are
ignored. Nothing here writes to the database — the vault stays a mirror.
"""

import re
from datetime import date, datetime, time, timezone
from typing import Optional

from app.models.trade_campaigns import TradeCampaign
from app.schemas.journal import JournalFeedEntry

_H2 = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_H3 = re.compile(r"^###\s+(?P<rest>.+?)\s*$")
# Hand-typed headings: em/en dash or hyphen separators, `2026-04-XX`
# placeholder days, optional title.
_DATED_H3 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-(?:\d{2}|[Xx]{2}))\s*(?:[—–-]+\s*(?P<title>.+?))?\s*$"
)

_REVIEW_SECTIONS = {"exit / review", "exit/review", "exit review", "review", "exit"}


def _entry_date(raw: Optional[str], fallback: date) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass  # `2026-04-XX` placeholder or typo
    return fallback


def _to_datetime(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def parse_campaign_entries(
    campaign: TradeCampaign, today: Optional[date] = None
) -> list[JournalFeedEntry]:
    """Project one campaign body into virtual journal-feed entries.

    Pure and defensive: bodies are hand-written, so anything that doesn't
    match the expected shape is skipped rather than raised.

    Future-dated Timeline sections (planned exits like "2026-09-18 — Call
    Expiry") are held back until their date arrives — otherwise they'd pin
    the top of the reverse-chronological feed for months.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    section: Optional[str] = None
    thesis_lines: list[str] = []
    review_lines: list[str] = []
    timeline: list[dict] = []
    current: Optional[dict] = None

    for line in campaign.body.splitlines():
        h2 = _H2.match(line)
        if h2:
            if current is not None:
                timeline.append(current)
                current = None
            section = h2.group("name").strip().lower()
            continue
        if section == "timeline":
            h3 = _H3.match(line)
            if h3:
                if current is not None:
                    timeline.append(current)
                dated = _DATED_H3.match(h3.group("rest"))
                current = {
                    "date": dated.group("date") if dated else None,
                    "title": dated.group("title") if dated else h3.group("rest"),
                    "lines": [],
                }
            elif current is not None:
                current["lines"].append(line)
        elif section == "thesis":
            thesis_lines.append(line)
        elif section in _REVIEW_SECTIONS:
            review_lines.append(line)
    if current is not None:
        timeline.append(current)

    fallback = campaign.opened_on or campaign.synced_at.date()
    entries: list[JournalFeedEntry] = []

    def _add(suffix: str, entry_type: str, title: Optional[str], content: str, on: date):
        if on > today:
            return
        entries.append(
            JournalFeedEntry(
                id=f"vault:{campaign.id}:{suffix}",
                source="vault",
                campaign_id=campaign.id,
                ticker=campaign.ticker,
                title=title,
                entry_type=entry_type,
                content=content,
                created_at=_to_datetime(on),
                updated_at=campaign.updated_at,
            )
        )

    thesis = "\n".join(thesis_lines).strip()
    if thesis:
        _add("thesis", "thesis", campaign.title, thesis, fallback)

    for i, item in enumerate(timeline):
        content = "\n".join(item["lines"]).strip()
        if not content and not item["title"]:
            continue
        _add(
            f"timeline:{i}",
            "note",
            item["title"],
            content,
            _entry_date(item["date"], fallback),
        )

    review = "\n".join(review_lines).strip()
    if review:
        _add("review", "review", campaign.title, review, campaign.closed_on or fallback)

    return entries
