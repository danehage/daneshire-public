#!/usr/bin/env python3
"""One-way sync of DaneBrain vault trade journals to the Daneshire app (#61).

Reads every campaign file under <vault>/Open and <vault>/Closed and POSTs the
whole listing to /api/trade-campaigns/sync with owner Basic auth. Idempotent —
safe to run from launchd (Mac), Task Scheduler (PC), or by hand; unchanged
files are skipped server-side, and campaigns whose files disappeared are
soft-archived.

Stdlib only (no venv needed). Configuration via environment:

  DANESHIRE_URL        app base URL   (default: prod Cloud Run URL)
  DANESHIRE_USERNAME   owner Basic auth username  } fall back to AUTH_USERNAME /
  DANESHIRE_PASSWORD   owner Basic auth password  } AUTH_PASSWORD in backend/.env
  DANEBRAIN_TRADES_DIR vault trades dir (default: ~/Developer/DaneBrain/70-Trades)
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://your-service.us-central1.run.app"
DEFAULT_VAULT = Path.home() / "Developer" / "DaneBrain" / "70-Trades"


def _load_env_fallback() -> dict:
    """Pull AUTH_USERNAME/AUTH_PASSWORD from backend/.env when env is unset."""
    env_path = Path(__file__).resolve().parent.parent / "backend" / ".env"
    values = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def main() -> int:
    vault = Path(os.environ.get("DANEBRAIN_TRADES_DIR", DEFAULT_VAULT))
    base_url = os.environ.get("DANESHIRE_URL", DEFAULT_URL).rstrip("/")

    fallback = _load_env_fallback()
    username = os.environ.get("DANESHIRE_USERNAME") or fallback.get("AUTH_USERNAME")
    password = os.environ.get("DANESHIRE_PASSWORD") or fallback.get("AUTH_PASSWORD")
    if not username or not password:
        # Local dev runs without auth configured; prod will reject with 401.
        print(
            "sync_vault: no credentials found — sending unauthenticated "
            "(fine for local dev; set DANESHIRE_USERNAME/DANESHIRE_PASSWORD "
            "or AUTH_USERNAME/AUTH_PASSWORD in backend/.env for prod)",
            file=sys.stderr,
        )

    files = []
    for folder in ("Open", "Closed"):
        folder_path = vault / folder
        if not folder_path.is_dir():
            continue
        for md in sorted(folder_path.glob("*.md")):
            files.append(
                {
                    "path": f"{folder}/{md.name}",
                    "content": md.read_text(encoding="utf-8"),
                }
            )

    if not files:
        print(f"sync_vault: no campaign files found under {vault} — nothing sent")
        return 1

    headers = {"Content-Type": "application/json"}
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(
        f"{base_url}/api/trade-campaigns/sync",
        data=json.dumps({"files": files}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"sync_vault: HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"sync_vault: connection failed: {exc.reason}", file=sys.stderr)
        return 1

    print(
        f"sync_vault: {len(files)} files -> "
        f"upserted={len(result['upserted'])} unchanged={result['unchanged']} "
        f"archived={len(result['archived'])} "
        f"watchlist_updated={','.join(result['watchlist_updated']) or 'none'}"
    )
    for path, error in result.get("errors", {}).items():
        print(f"sync_vault: PARSE WARNING {path}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
