"""One-way sync of DaneBrain vault trade journals into trade_campaigns (#61).

The vault (``~/Developer/DaneBrain/70-Trades/{Open,Closed}``) is authoritative.
Each sync call carries the complete current file listing; this module parses
frontmatter defensively (files are hand-written), upserts by filename stem,
soft-archives campaigns whose files disappeared, and — for open campaigns —
upserts the matching watchlist item so the plateau-grade engine can resolve
open dates (the #57 gap-gauge dependency).
"""

import hashlib
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Optional

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_campaigns import TradeCampaign
from app.models.watchlist import WatchlistItem, active_first_order
from app.schemas.trade_campaigns import VaultSyncFile, VaultSyncResponse

logger = logging.getLogger(__name__)

# Frontmatter `strategy` values that map onto watchlist position_type. Other
# strategies leave position_type untouched.
_STRATEGY_TO_POSITION_TYPE = {
    "long_shares": "long",
    "wheel": "cash_secured_put",
    "covered_call": "covered_call",
}


def _parse_number(value) -> Optional[Decimal]:
    """Frontmatter numbers are hand-typed: '2,458.01', 29925, '', None."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_date(value) -> Optional[date]:
    """YAML parses bare ISO dates natively; hand-typed variants come as str."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def split_frontmatter(content: str) -> tuple[dict, str, Optional[str]]:
    """Return (frontmatter, body, error). Never raises.

    On any parse problem the full content becomes the body and the error is
    reported per-file rather than failing the batch.
    """
    if not content.startswith("---\n"):
        return {}, content, "no frontmatter block"
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content, "unterminated frontmatter block"
    raw = content[4:end]
    body = content[end + 5 :]
    try:
        fm = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return {}, content, f"frontmatter YAML error: {exc}"
    if not isinstance(fm, dict):
        return {}, content, "frontmatter is not a mapping"
    return fm, body, None


def _jsonable(fm: dict) -> dict:
    """Frontmatter for the JSONB column — dates to ISO strings."""
    out = {}
    for k, v in fm.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


class VaultSyncService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def sync(self, files: list[VaultSyncFile]) -> VaultSyncResponse:
        now = datetime.now(timezone.utc)
        response = VaultSyncResponse()

        existing_result = await self.db.execute(select(TradeCampaign))
        existing = {row.title: row for row in existing_result.scalars().all()}

        # A campaign file can transiently exist in both Open/ and Closed/
        # (mid-move sync). Later files overwrite earlier ones, so process
        # Open/ first (reverse-alphabetical: 'Open/…' > 'Closed/…') — a
        # campaign that reached Closed/ is closed.
        seen_titles: set[str] = set()
        for f in sorted(files, key=lambda f: f.path, reverse=True):
            path = PurePosixPath(f.path.replace("\\", "/"))
            title = path.stem
            folder = path.parts[0].lower() if len(path.parts) > 1 else ""
            if folder not in ("open", "closed"):
                response.errors[f.path] = (
                    "not under Open/ or Closed/ — skipped"
                )
                continue
            status = "open" if folder == "open" else "closed"
            seen_titles.add(title)

            content_hash = hashlib.sha256(f.content.encode()).hexdigest()
            row = existing.get(title)
            if (
                row is not None
                and row.content_hash == content_hash
                and row.status == status
            ):
                response.unchanged += 1
                continue

            fm, body, error = split_frontmatter(f.content)
            if error:
                response.errors[f.path] = error

            ticker = str(fm.get("ticker") or title.split("-")[0]).upper()[:10]
            opened_on = _parse_date(fm.get("opened"))
            closed_on = _parse_date(fm.get("closed"))
            cost_basis = _parse_number(fm.get("cost_basis"))
            realized_pnl = _parse_number(fm.get("realized_pnl"))
            account = str(fm["account"])[:30] if fm.get("account") else None
            strategy = str(fm["strategy"])[:30] if fm.get("strategy") else None

            if row is None:
                row = TradeCampaign(title=title, body="", content_hash="")
                self.db.add(row)
                # Register in the map so a same-title duplicate later in this
                # batch (mid-move: file in both folders) updates this row
                # instead of inserting a second one.
                existing[title] = row
            row.vault_path = str(path)
            row.ticker = ticker
            row.status = status
            row.frontmatter = _jsonable(fm)
            row.body = body
            row.content_hash = content_hash
            row.opened_on = opened_on
            row.closed_on = closed_on
            row.cost_basis = cost_basis
            row.realized_pnl = realized_pnl
            row.account = account
            row.strategy = strategy
            row.parse_error = error
            row.synced_at = now
            row.updated_at = now
            response.upserted.append(f.path)

            if status == "open":
                updated = await self._upsert_watchlist(
                    ticker, opened_on, cost_basis, strategy
                )
                if updated:
                    response.watchlist_updated.append(ticker)

        # Soft-archive campaigns whose files disappeared from the vault.
        for title, row in existing.items():
            if title not in seen_titles and row.status != "archived":
                row.status = "archived"
                row.updated_at = now
                response.archived.append(title)

        await self.db.commit()
        logger.info(
            "vault-sync: upserted=%d unchanged=%d archived=%d errors=%d",
            len(response.upserted),
            response.unchanged,
            len(response.archived),
            len(response.errors),
        )
        return response

    async def _upsert_watchlist(
        self,
        ticker: str,
        opened_on: Optional[date],
        cost_basis: Optional[Decimal],
        strategy: Optional[str],
    ) -> bool:
        """Mark an open campaign's ticker as owned with its entry facts.

        This is what lets the plateau-grade engine resolve the position open
        date (#57): run anchoring + the gap gauge read entry_date from the
        watchlist. Never downgrades data — only fills/updates from the vault,
        which is authoritative for entries.
        """
        # Active rows first, then oldest, matching snapshots/commit: ticker
        # has no unique constraint, so with duplicates an unordered .first()
        # could promote a different row each sync, and a plain oldest-first
        # would reopen a closed row beside the active one.
        result = await self.db.execute(
            select(WatchlistItem)
            .where(WatchlistItem.ticker == ticker)
            .order_by(*active_first_order())
        )
        item = result.scalars().first()
        changed = False
        if item is None:
            item = WatchlistItem(ticker=ticker, status="owned")
            self.db.add(item)
            changed = True
        elif item.status != "owned":
            item.status = "owned"
            changed = True
        if opened_on is not None and item.entry_date != opened_on:
            item.entry_date = opened_on
            changed = True
        if cost_basis is not None and item.entry_price != cost_basis:
            item.entry_price = cost_basis
            item.cost_basis = cost_basis
            changed = True
        position_type = _STRATEGY_TO_POSITION_TYPE.get(strategy or "")
        if position_type and item.position_type != position_type:
            item.position_type = position_type
            changed = True
        return changed
