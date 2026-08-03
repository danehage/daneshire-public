"""Read-only prod check for the 2026-08-03 dashboard investigation.

Prints status distributions the dashboard summary depends on. SELECTs only.
Usage: NEON_DATABASE_URL in env (asyncpg URL), then `python scripts/dashboard_ground_truth.py`.
"""

import asyncio
import os
import ssl
from urllib.parse import urlsplit

import asyncpg


async def main():
    url = os.environ["NEON_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    host = urlsplit(url).hostname
    print(f"host: {host}")
    ctx = ssl.create_default_context()
    conn = await asyncpg.connect(url.split("?")[0], ssl=ctx)
    try:
        for label, q in [
            ("watchlist by status", "SELECT status, count(*) FROM watchlist_items GROUP BY status ORDER BY status"),
            ("alerts by status", "SELECT status, count(*) FROM alerts GROUP BY status ORDER BY status"),
            ("journal_entries", "SELECT count(*) FROM journal_entries"),
            ("portfolio_snapshots", "SELECT count(*) FROM portfolio_snapshots"),
            ("accounts", "SELECT count(*) FROM accounts"),
            ("trade_campaigns by status", "SELECT status, count(*) FROM trade_campaigns GROUP BY status ORDER BY status"),
        ]:
            rows = await conn.fetch(q)
            print(f"{label}: {[tuple(r) for r in rows]}")
    finally:
        await conn.close()


asyncio.run(main())
