# Daneshire Hathaway

Stock research terminal for swing trading and options strategies. Single-user app with watchlist management, trade journaling, quantitative scanner, and price alerts with push notifications.

**Live:** https://your-service.us-central1.run.app (HTTP Basic Auth required)

Two auth roles: the **owner** credentials unlock everything; optional **guest** credentials (`GUEST_USERNAME`/`GUEST_PASSWORD`) get read-only access to the scanner, earnings calendar, and ticker context — portfolio, watchlist, journal, trades, and alerts stay owner-only.

## Features

- **Dashboard** — Summary cards, recent activity feed, journal search, quick actions
- **Watchlist** — Track tickers with drag-and-drop reordering, status tabs (Watching/Owned/Open/Closed). Portfolio holdings merge in automatically as Owned
- **Price Targets** — Set entry/exit targets per ticker
- **Journal** — Trade notes with type badges (thesis, entry, exit, adjustment, review), standalone `/journal` page, and full-text search
- **Scanner** — Find stocks meeting technical criteria across selectable universes (S&P 500 + S&P 400 combined by default, ~900 stocks, $20 price floor), with real-time SSE progress and progressive score-sorted results mid-scan. Two-track scoring favors stabilization over damage
- **Ticker Detail** — Full technical analysis (RSI, HV Rank, support/resistance, moving averages) plus a hybrid context block: fundamentals + a Gemini-generated narrative
- **Portfolio** — screenshot-driven holdings: upload a broker screenshot (any brokerage), Gemini vision parses positions/cash/account into an editable review pane, commit persists. Multi-account with aggregate view, live FMP marks, day change, cash, value-over-time chart. Short positions and options (decomposed from broker description strings) supported. Held equities get sell-side plateau grades (HV and ATR% surfaced in the grade column) with stop suggestions
- **Trades** — trade campaigns synced one-way from the DaneBrain Obsidian vault (`70-Trades/{Open,Closed}`): `/trades` list + per-campaign detail pages. The vault is authoritative; sync upserts by filename and soft-archives removed campaigns
- **Earnings Calendar** — Upcoming earnings dates from Finnhub with sortable views, date filtering, expected-move calculations (Tastytrade IV), and an earnings trade journal
- **Alerts** — Price and technical condition alerts with Pushover notifications
- **Alert Engine** — Cloud Scheduler jobs: price/technical checks every 15 min, hourly sell-side plateau grading, daily earnings and reminder runs
- **Backups** — nightly `pg_dump` of the production database to GCS

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 18 + Vite + Tailwind CSS + TanStack Query + Recharts |
| Backend | FastAPI + Uvicorn (async) |
| Database | Neon Postgres (managed) |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| Notifications | Pushover |
| Quotes / Historicals | Financial Modeling Prep (FMP) |
| Earnings calendar | Finnhub |
| Brokerage / Options IV | Tastytrade (OAuth2) |
| Screenshot parsing | Gemini (`gemini-2.5-flash`, JSON mode) |
| Deploy | GCP Cloud Run + Cloud Scheduler |

## Local Development

### Prerequisites

- Python 3.12+
- Node.js 20+
- Neon Postgres database
- FMP API key (quotes, historicals)
- Finnhub API key (earnings calendar)
- Tastytrade OAuth credentials (options IV) — optional, see DEPLOY.md
- Gemini API key (portfolio screenshot parsing) — optional, get one at https://aistudio.google.com/apikey
- Pushover keys (alert notifications) — optional

### Setup

```bash
# Clone
git clone https://github.com/danehage/daneshire.git
cd daneshire

# Backend
cd backend
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt  # runtime deps + pytest

# Create backend/.env (see .env.example at the repo root for the full template).
# Settings load .env from the working directory, so a bare `uvicorn` run from
# backend/ reads backend/.env; docker-compose reads the repo-root .env instead.
# Point NEON_DATABASE_URL at a dev/test branch, never production: the test
# suite's cleanup fixtures delete rows (tests/conftest.py blocks the prod endpoint).
cat > .env << EOF
NEON_DATABASE_URL=postgresql+asyncpg://user:pass@host/db?ssl=require
FMP_API_KEY=your_fmp_key
FINNHUB_API_KEY=your_finnhub_key
TASTYTRADE_CLIENT_SECRET=your_tastytrade_oauth_client_secret
TASTYTRADE_REFRESH_TOKEN=your_tastytrade_oauth_refresh_token
GEMINI_API_KEY=your_gemini_key
PUSHOVER_USER_KEY=your_user_key
PUSHOVER_API_TOKEN=your_app_token
SCHEDULER_SECRET=any_random_string
ENVIRONMENT=development
# Auth disabled in development; set these for local testing:
# AUTH_USERNAME=your_username
# AUTH_PASSWORD=your_password
# Optional read-only guest role (scanner + earnings + ticker context):
# GUEST_USERNAME=guest_username
# GUEST_PASSWORD=guest_password
EOF

# Run migrations
alembic upgrade head

# Start backend
uvicorn app.main:app --reload --port 8000
```

```bash
# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173

### Running Tests

```bash
cd backend
pytest tests/ -v
```

~550 tests covering:
- Watchlist, journal, alerts CRUD
- Alert condition evaluator
- Internal scheduler endpoints (price/technical/reminder/expire/plateau)
- Portfolio engine + live mark math, trades + layered compute, value history, plateau grades
- Snapshot parse endpoint (FakeVisionParser): diff classification, error mapping, parse→commit round trip, short positions
- Earnings calendar + earnings trade journal, calendar date-drift handling and duplicate-event merge
- Market seam: Finnhub client, Tastytrade client, IV / realized-move calculations, IV-snapshot skip reasons
- Scanner universes + two-track rescore
- Vault trade-campaign sync (frontmatter parsing, upsert/archive, watchlist linkage)
- Guest-role auth gating

**CI:** GitHub Actions (`.github/workflows/ci.yml`) runs on every push to `master`. It runs the backend suite against a service-container Postgres, the frontend build, and an advisory dependency audit. CI runs alongside the deploy but doesn't gate it.

## Production Deployment

**Push to `master` to deploy.** A Cloud Build trigger runs [`cloudbuild.yaml`](cloudbuild.yaml), which builds the image, applies Alembic migrations, and deploys to Cloud Run. The deploy step pins the dedicated runtime service account and the full Secret Manager `--set-secrets` list. Don't hand-run `gcloud run deploy`: it would drop that configuration.

See [DEPLOY.md](DEPLOY.md) for first-time GCP setup (secrets, service accounts, Cloud Scheduler jobs).

The production image installs from `backend/requirements.lock` (hash-pinned, `--require-hashes`) and runs as an unprivileged `app` user. `requirements.txt` (runtime) and `requirements-dev.txt` (tests/dev, not in the image) stay the source of truth for direct dependencies; the locks must be compiled on Linux + Python 3.12 (a Windows compile drops uvloop). To change dependencies, edit `requirements*.txt` on a branch, push, then run the **Regenerate locks** workflow on that branch (GitHub → Actions → Regenerate locks → Run workflow, or `gh workflow run regenerate-locks.yml --ref <branch>`); it commits both updated locks back to the branch. CI installs `requirements-dev.lock`.

Production requires HTTP Basic Auth credentials (configured via `AUTH_USERNAME` and `AUTH_PASSWORD` secrets).

## Project Structure

```
daneshire/
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI app
│   │   ├── config.py         # Settings
│   │   ├── database.py       # Async SQLAlchemy
│   │   ├── models/           # SQLAlchemy models
│   │   ├── schemas/          # Pydantic schemas
│   │   ├── routes/           # API endpoints
│   │   └── services/         # Business logic
│   ├── alembic/              # Migrations
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── api/              # API client functions
│   │   ├── components/       # React components
│   │   ├── hooks/            # React Query hooks
│   │   └── pages/            # Route pages
│   └── package.json
├── docs/adr/                 # Architecture decision records
├── legacy/                   # v1 codebase (scanner/indicators source)
├── .github/workflows/ci.yml  # GitHub Actions CI
├── Dockerfile                # Production multi-stage build
├── cloudbuild.yaml           # Cloud Build: build, migrate, deploy on push to master
├── DEPLOY.md                 # Deployment guide
├── ARCHITECTURE.md           # Database schema & API design
├── CONTEXT.md                # Domain glossary
├── PRINCIPLES.md             # Investment analysis lens
├── TODO.md                   # Current status + latest session log
└── TODO_ARCHIVE.md           # Past session logs
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/health/db` | Database connectivity check |
| GET | `/api/me` | Current user's role (owner / guest) |
| GET | `/api/dashboard/summary` | Dashboard stats |
| GET/POST | `/api/watchlist` | List/create watchlist items |
| GET/PATCH/DELETE | `/api/watchlist/{id}` | Get/update/delete item |
| POST | `/api/watchlist/reorder` | Reorder items |
| GET/POST | `/api/watchlist/{id}/targets` | Price targets |
| GET/POST | `/api/watchlist/{id}/journal` | Journal entries for a watchlist item |
| GET/POST | `/api/journal` | List/create journal entries (standalone) |
| GET | `/api/journal/search` | Full-text journal search |
| GET | `/api/scan/universes` | Available stock universes with sizes |
| POST | `/api/scan/execute` | Run scanner |
| GET | `/api/scan/{id}/stream` | SSE progress |
| GET | `/api/scan/{id}/results` | Scan results (partial mid-scan, score-sorted) |
| GET | `/api/scan/ticker/{symbol}` | Single-ticker technical analysis |
| GET | `/api/ticker/{symbol}/context` | Fundamentals context block |
| GET | `/api/ticker/{symbol}/context/narrative` | Gemini-generated narrative |
| GET/POST | `/api/alerts` | List/create alerts |
| GET/PATCH/DELETE | `/api/alerts/{id}` | Get/update/delete alert |
| POST | `/api/alerts/{id}/dismiss` | Dismiss alert |
| GET | `/api/alerts/{id}/history` | Evaluation history for an alert |
| POST | `/api/alerts/evaluate` | Run the alert engine on demand (one type or all) |
| GET | `/api/portfolio` | Positions, live marks, day change, cash (all accounts aggregated, or `?account_id=`) |
| GET | `/api/portfolio/accounts` | List brokerage accounts |
| POST | `/api/portfolio/snapshots/parse` | Parse portfolio screenshot via Gemini (stateless) |
| POST | `/api/portfolio/snapshots/commit` | Persist reviewed snapshot + holdings |
| POST | `/api/portfolio/trades/parse` | Parse a trade-fill screenshot via Gemini (stateless) |
| POST | `/api/portfolio/trades/commit` | Record a trade fill |
| GET | `/api/portfolio/trades` | Trade log |
| GET | `/api/portfolio/value-history` | Sparse value series (snapshots + live point) |
| GET | `/api/trade-campaigns` | Trade campaigns (vault-synced) |
| GET | `/api/trade-campaigns/{id}` | Campaign detail with journal body |
| POST | `/api/trade-campaigns/sync` | One-way vault sync (upsert + soft-archive) |
| GET | `/api/earnings/calendar` | Upcoming earnings (date range) |
| GET | `/api/earnings/screen` | Earnings screen with IV / expected-move data |
| GET | `/api/earnings/{ticker}` | Per-ticker earnings view (next event, latest IV, realized moves, edge ratio, recent trades) |
| GET | `/api/earnings/{ticker}/expected-move` | Expected move for an upcoming event |
| GET/POST | `/api/earnings/trades` | Earnings trade journal |

## Scheduled Jobs

Cloud Scheduler drives all background work via `/api/internal/*` endpoints (authenticated with `X-Scheduler-Secret`):

| Job | Schedule | Action |
|-----|----------|--------|
| Price checks | Every 15 min, 9-16 ET Mon-Fri | Evaluate price alerts |
| Technical checks | Every 15 min, 9-16 ET Mon-Fri | Evaluate RSI/HV alerts |
| Plateau checks | Hourly, 10-16 ET Mon-Fri | Grade held equities (sell-side), ping on grade worsening |
| Reminders | 8 AM daily | Send date reminders |
| Expire stale | Midnight daily | Mark expired alerts |
| Earnings calendar refresh | 10 PM daily | Upsert 4-week earnings calendar from Finnhub |
| IV snapshots | 3:45 PM ET Mon-Fri | Snapshot options IV from Tastytrade for upcoming earnings (before the close, so illiquid names still have two-sided quotes) |
| Earnings alert checks | 5 PM ET Mon-Fri | Evaluate earnings alerts |
| DB backup | 2 AM daily | `pg_dump` of the Neon database uploaded to GCS |

Notifications are sent via Pushover with priority levels (low, normal, high, urgent).

## License

Private project.
