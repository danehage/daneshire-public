# Context — Danecast Trades

Domain glossary. Use these terms exactly when naming code, writing issues, or describing behaviour. New terms are added here as design decisions crystallise.

## Alerts

**Alert** — a user-defined rule attached (optionally) to a watchlist item, evaluated on a schedule. Stored in the `alerts` table.

**Alert Evaluation** — the process of deciding whether an Alert's Condition is currently met, recording the result in `alert_history`, and (if met) dispatching a notification. Lives in `backend/app/services/alert_engine.py`. Single home for all four alert flavours.

**Condition** — the structured rule inside an Alert. A discriminated union over `alert_type`:

- `PriceCondition` — `{metric: "price", operator, value}`
- `TechnicalCondition` — `{metric: "rsi" | "hv_rank" | ..., operator, value}`
- `EarningsCondition` — `{metric: "eps", operator, value, trigger_date}`
- `ReminderCondition` — `{trigger_date}`
- `CustomCondition` — reserved; not yet modelled

Each variant owns its own `.evaluate(observation) -> Outcome` and `.format() -> str`. Operator + metric vocabularies live behind this interface, not in the Pydantic schema layer or a separate `OPERATORS` dict.

**Observation** — the typed input to a Condition's evaluation. One shape per Condition variant (e.g. a price quote for `PriceCondition`, an earnings record for `EarningsCondition`, `None` for `ReminderCondition`). Produced by the **orchestrator** in batch per alert_type (see below), consumed by the evaluator.

**Outcome** — the result of one Alert × one Observation. A discriminated union: `Met(actual_value)`, `NotMet(actual_value)`, `Errored(reason)`. The evaluator's interface is narrowed to `Met | NotMet` — it cannot produce `Errored`. The orchestrator constructs `Errored` when a fetch raises or a batch omits a ticker. All three are written 1:1 into `alert_history`.

**Orchestrator** (inside Alert Evaluation) — `engine.run(alert_type)`. Loads active alerts of that type, batch-fetches Observations (one bulk call per type where possible), pairs each alert with its Observation (or constructs `Errored`), invokes the variant's `.evaluate(obs)`, writes one `alert_history` row, and on `Met` sends a Pushover via the notifier. The `match alert_type:` branch for unimplemented types (currently `earnings_check`, `custom`) falls through to `Errored("not yet implemented")` — engine never crashes on an unknown type.

**RunSummary** — return value of `engine.run(alert_type)`. Counts of `met / not_met / errored`, plus the per-alert outcome list for debugging via the manual `POST /api/alerts/evaluate` endpoint.

## Watchlist

**Watchlist item** — a tracked ticker in `watchlist_items`. Always has a status (`watching | position_open | closed`). The anchor that price targets, journal entries, and alerts hang off.

## Market Data

**MarketData** — the single in-process seam for all FMP traffic. Owns one `FMPClient` (shared throttle + retry) and one `StockScanner` (delegated to for `analysis`). Constructed once in FastAPI's `lifespan`, injected as a dependency. Interface: `quote(ticker)`, `quotes(tickers)`, `analysis(ticker)`, `analyses(tickers)`, `history(ticker, days)`. Batch methods return `dict[ticker, T | MarketDataError]` — per-ticker failures are explicit, not exceptions. Lives in `backend/app/services/market.py`.

**Observation** types (from the Alerts section above) are produced here: `PriceQuote`, `TechnicalAnalysis`, `HistoryFrame`, frozen Pydantic models in `app/schemas/market.py`.

**MarketData cache** — in-process TTL cache, keyed by (kind, ticker). Quotes ~10s, analyses ~5min, history ~1hr. Each cache is bounded (per-kind `maxsize`, LRU eviction, expired entries swept on insert) so the single 512Mi instance can't grow it without limit. Reads are single-flight per key (concurrent identical requests await the first in-flight fetch instead of stampeding FMP). Writes don't flow through MarketData, so manual invalidation isn't needed.

**MarketDataError** — single exception hierarchy: `TickerNotFound`, `RateLimited`, `UpstreamError`. Returned per-ticker from batch methods; raised from singleton methods.

**Deferred** — migrating the scanner's Stage 2 (`ThreadPoolExecutor` over `_process_ticker`) to consume MarketData. Today the scanner has its `FMPClient` injected by MarketData, so the throttle is already shared. Stage 2's parallelism shape is unchanged until a follow-up converts it to `asyncio.gather` with a semaphore.

## Portfolio

**Account** — an externally-managed brokerage container (Roth, taxable, IRA, UTMA). Stored in `accounts`. Seeded lazily — the first snapshot commit naming an unknown account creates the row.

**Portfolio Snapshot** — an immutable record of an Account's positions + cash at a point in time, parsed from a single screenshot. Stored in `portfolio_snapshots`. `captured_at` is the screenshot's wall-clock moment (user-editable in the review pane), not the upload time.

**Holding** — an immutable per-position row attached to a Portfolio Snapshot (FK, cascade delete). Captures `instrument_type` (equity | option), ticker, qty, avg_cost, market_value at snapshot time, and option-specific nullable fields (strike, expiry, option_type, multiplier, underlying_ticker). **Negative qty is a short position** (sold options, short stock); zero qty is invalid.

**Trade** — an append-only record of one buy or sell fill. Stored in `trades`. Buys update running average cost; sells emit realized P/L as `(sell_price − avg_cost) × qty`. Average cost only — not tax-lot tracking.

**Portfolio Engine** — orchestrator owning the layered model: current holdings = (latest snapshot per account) + (trades after that snapshot's `captured_at`). Lives in `backend/app/services/portfolio_engine.py`. Mirrors the Alert Engine pattern (orchestrator over the `MarketData` seam for live marks). `GET /api/portfolio` without `account_id` aggregates all accounts; `last_snapshot_at` on the aggregate is the *stalest* account's latest snapshot, so the freshness nudge reflects the account most in need of a re-upload.

**Vision Parser** — single seam for screenshot-to-structured-data extraction. One adapter (`GeminiVisionParser`, `gemini-2.5-flash` in enforced JSON mode) behind a `VisionParser` protocol; the protocol exists for test injection (per ADR-0001), not vendor-swap futures. Parse is stateless (`POST /snapshots/parse` never writes); commit persists. Broker option description strings ("ACME Sep 18 '26 $100 Call") are decomposed into ticker + option fields — `ticker` is only ever the underlying symbol. On schema-validation failure the adapter retries once, feeding the Pydantic error back to the model. Error hierarchy: `VisionLowConfidence` → 422, `VisionRateLimited` → 429, `VisionUpstreamError` → 502, parser unconfigured → 503.

**Owned Position** — *pending (#10)*: a watchlist item whose status is `owned`, auto-promoted when a snapshot commit includes its ticker.

**Sell-side Grade (#57)** — per-holding plateau grade answering "has the rising stopped." Six labels in precedence order: Broken (below the newest confirmed swing floor or underwater vs entry base) > Climax (RSI ≥ 75 at the newest run high AND ≥ 15% extension over the 50-MA) > Thesis complete (price ≥ 88% of the pre-entry 52-week high) > Distribution > Cresting > Running. Metrics in `calculate_plateau_metrics()` (`indicators.py`), run-anchored to max(open date, trailing 126 bars); the RSI delta compares the newest *swing high* against the prior ceiling (lower swing highs never become ceilings — that's the fade signal). Graded hourly during market hours (10:00–16:00 ET, swing cadence) by `POST /api/internal/portfolio/run-plateau-checks`, logged to `holding_grade_history` (append-only), surfaced on the Portfolio page. Labels (not score) drive Pushover: any worsening + one daily refresher while deteriorated; silent on recovery. Stops: structure stop under the newest minor swing floor (only when ≤ 12% away) + 1.5× ATR(14) trail — never flat $/%. HV and days-since-high are display-only, never scored. Acceptance fixtures: real frozen bars for historical reference exits in `tests/fixtures/plateau/`.

**Trade Campaign (#61)** — a vault trade journal synced into `trade_campaigns` and shown on `/trades`. **The DaneBrain Obsidian vault is authoritative** (`~/Developer/DaneBrain/70-Trades/{Open,Closed}`, one file per campaign, YAML frontmatter); the app is a read-only mirror and never writes back. One-way whole-vault sync via `scripts/sync_vault.py` (launchd every ~20 min on the Mac; Obsidian Sync funnels PC edits there; script is idempotent and can also run on the PC). Keyed by filename stem because files move `Open/` → `Closed/`; files that disappear are soft-archived. Frontmatter parses defensively (hand-typed commas, `2026-04-XX` placeholders → null) with per-file errors surfaced, never batch failures. Side effect: open campaigns upsert their watchlist item (`status=owned`, `entry_date` from `opened:`, `entry_price` from `cost_basis:`) — this is what feeds the Sell-side Grade's run anchoring and gap gauge.

## Journal

**Journal Feed** — the merged list served by `GET /api/journal`: app-authored `journal_entries` rows plus **virtual entries** projected at read time from synced Trade Campaign bodies (`## Thesis` → `thesis`, dated `### ` Timeline sections → `note`, `## Exit / Review` → `review`; other `##` sections ignored). Vault-sourced entries are read-only — synthetic `vault:{campaign_id}:{section}` ids, `source="vault"`, never copied into `journal_entries` (the vault stays authoritative, per Trade Campaign). Archived campaigns don't feed it. **Future-dated Timeline sections (planned exits) intentionally sort to the top** — Dane wants upcoming plans visible above the history (decided 2026-08-03; a hold-back-until-dated variant was shipped and reverted same day — don't reintroduce it). Parser: `backend/app/services/campaign_journal.py`. Decisions (#119) also join the feed as read-only virtual entries (`source="decision"`, id `decision:{id}`, `entry_type="decision"`).

**Share Mode** — frontend-only toggle (Layout footer, persisted in localStorage) that masks Dane's own money for showing the app to someone: balances/market values/P/L dollars → `$XX`, share/contract quantities → `yyy`, and all `$` amounts inside free-text journal markdown. Market data stays visible: per-share prices (avg cost, entry price, fills), percentages, indicators, scanner/earnings output. Nothing server-side changes — masking is render-time only (`frontend/src/hooks/useShareMode.jsx`). Known gap: quantities written in prose ("Bought 100 ACME") are not masked.

## Decision log

**Decision** — a buy or pass call on a ticker, with conviction 1–5, a one-line thesis and what would prove it wrong (#119). Stored in `decisions`, **insert-only** (a DB trigger rejects updates). `price_at_decision` and `spy_at_decision` come from one server-side quote request when it's recorded; the client can't set prices or the time. Deletable within 15 minutes for typos, never after. A change of mind is a new decision with `supersedes_id` set; both count in the stats. No backfill: a call entered after the fact is written with hindsight.

**Decision outcome** — one decision × one horizon (21, 63 or 126 sessions), stored in `decision_outcomes` (insert-only). `excess_return` = ticker price return − SPY price return from the decision quotes to the Nth SPY close after the decision. Passes are not sign-flipped: a positive number on a pass is a winner passed on. Status `scored`, `truncated` (ticker data ended; scored at its last close against SPY to the same date) or `no_data` (excluded from stats). Scored by `POST /api/internal/decisions/score` (`backend/app/services/decision_scoring.py`).

**Sessions** (decision log) — counted on SPY's own daily bars, not a market calendar (ADR-0001): session 1 is the first SPY close after the decision, so holidays fall out automatically.

## Scanner

**Scan universe** — the ticker list a scan starts from. The default, `combined`, is built from FMP's company screener since #118 (`backend/app/services/screener_universe.py`): NYSE/NASDAQ/AMEX (+ CBOE, for Cboe itself) listings ≥ $2B, US-domiciled or a current S&P 500/400 member (34 index members are domiciled abroad but file 10-Qs: ACN, LIN, MDT, ETN, …); no preferreds, notes, warrants, units or rights (symbol suffix + company-name rules); no K-1 partnerships (legal name ends in LP, except the 1099-issuing HESM and PAGP); avgVolume × price ≥ $10M/day; share classes collapsed to the most liquid line. Symbols keep FMP's dash spelling (BRK-B): our plan answers 402 for the dotted BRK.B. Rebuilt weekly into a file next to the Stage-1 cache (per Cloud Run instance, so a fresh instance rebuilds on its first scan); if the screener fails, the last build of any age, then the static S&P 500 + 400 lists. Screener scans are recorded in `scan_picks` as universe `combined_screener`; a static-fallback scan stays `combined`, matching pre-#118 rows. `sp500` / `sp400` / `sp500_sample` remain static. **The quality gate's backtest evidence (ADR-0007) covers the S&P 500 only** — on the wider universe the gate is a filter, not a tested edge.

**HV Rank** — historical-volatility-rank proxy. Not implied volatility. UI labels must say "HV Rank", never "IV Rank".

**Fact columns** — since #117 the scanner is a quality-gated research shortlist, not a ranker. Each row shows sortable facts: % off the 52-week high, last earnings reaction vs SPY, last EPS beat/miss, FCF yield, FCF margin, ROIC, gross margin and its change over the prior four quarters (`services/quality_facts.py`; blank for financials and REITs), trend vs the 50/200-day MAs, HV Rank, days to next report, sector, market cap. Default sort is **Setup fit** (since 2026-10-15; before that ticker A→Z).

**Earnings reaction vs SPY** — signed % move from the close before the last report date to the close of the first session after it, minus SPY over the same two closes. FMP's earnings feed has no before-open/after-close flag, so one window covers both (exact for after-close reporters; one extra session for before-open ones). Empty until that next session closes.

**Setup fit** — the scanner's default sort since 2026-10-15: how many of Dane's four setup traits a row matches (0–4, equal weight, missing data = not met): 20–40% off the 52-week high, RSI higher low (`rsi_floor_delta > 0`), bounce holding (`post_low_trend == "rising"`), beat but dropped (EPS beat, earnings reaction vs SPY < 0). It is a **reading order** for 700+ gate passers, not a prediction: it describes the setups Dane researches and claims nothing about returns, so the #114 verdict doesn't bar it. Test it against the decision log (#119), not by tuning. Computed in the page (`SETUP_TRAITS` in `ScannerPage.jsx`); it does not order `scan_picks.rank`.

**Legacy score** — `StockScanner._score_opportunity`, the old composite fit to five trades. Its top 15 trailed SPY out-of-sample (ADR-0007 verdict, #114). Shown as the last column and still orders `scan_picks.rank`; remove ~2027-01-10. Never the default sort; do not tune it.
