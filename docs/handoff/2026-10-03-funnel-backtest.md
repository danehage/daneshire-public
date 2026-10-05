# Handoff: scanner audit → conviction funnel → backtest (2026-10-03)

Resume note for picking this work up on another machine. Start here, then
read `docs/adr/0007-conviction-funnel-and-backtest-gate.md`.

- **Branch:** `ccr-5911fb6b-njtpwd`, pushed, not merged, no PR open.
- **Tracking issue:** #109 (run the backtest on real FMP data).
- **Status:** everything is built and tested (730 tests pass). **The
  backtest has not run on real data**, so there is no verdict yet.

## Resume prompt

Paste this into Claude Code on your PC after checking out the branch:

> Read `docs/handoff/2026-10-03-funnel-backtest.md` and ADR-0007. My FMP
> key is in `backend/.env`. Run the funnel backtest per issue #109: check
> the plan first, then fetch, then run. Walk me through the report against
> the pre-registered rule. Don't tune any thresholds.

## Getting set up on the PC

```bash
git fetch origin
git checkout ccr-5911fb6b-njtpwd
cd backend
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head             # adds scan_picks + fundamentals_snapshots (migration e1f2a3b4c5d6)
```

**FMP key:** `TODO.md` says the local `backend/.env` key is dead (401 on
every endpoint). Rotate it first. The prod key lives in Secret Manager.
The backtest reads `FMP_API_KEY` from the environment:
- bash: `export FMP_API_KEY=...`
- PowerShell: `$env:FMP_API_KEY="..."`

Running scripts with `python-dotenv` doesn't apply here, so set the
variable explicitly.

## Running the backtest (from `backend/`)

```bash
python -m scripts.backtest.run --check-plan   # 1. which FMP endpoints our plan serves
python -m scripts.backtest.run --fetch        # 2. fill the cache, resumable (~8 calls x ~800 symbols)
python -m scripts.backtest.run                # 3. weekly replay 2016 -> today, ~30 min on 4 cores
```

- Output lands in `scripts/backtest/output/`: `report.md`, `picks.csv`,
  `diagnostics.csv`, `decision.json`. The cache and output dirs are
  gitignored.
- **What to check in step 1:**
  - Historical S&P 500 constituents are served.
  - Quarterly statements come back with about 60 rows.
  - `/stable/earnings` rows carry `epsActual`, `epsEstimated`,
    `revenueActual` and `revenueEstimated` (the field names are printed).
    These field names are assumptions that have **not been verified**
    against a live response.
  - Dividend-adjusted bars come back. If not, the harness falls back to
    `/full` automatically.
  - Unadjusted bars show AAPL near $110 in Jan 2015 (not ~$27), and
    historical market cap reaches back to 2014. The dollar thresholds
    depend on both (ADR-0007 amendment, 2026-10-05).
  - AAPL's oldest `weightedAverageShsOutDil` (printed on the income
    statement row) is split-adjusted (~26B; verified 2026-10-05). The
    market-cap fallback relies on that, so recheck if FMP changes it.
- **If historical constituents are missing,** run with
  `--universe current900`. The report labels that run survivorship-biased.
- **Useful flags:**
  - `--freq M` runs monthly, which is faster.
  - `--no-scanner` skips the current-scanner replay, the slowest variant.
  - `--synthetic` is a no-network pipeline check. Its numbers are noise.

## The decision rule (pre-registered — do not move the goalposts)

The rule is evaluated on out-of-sample dates only, from 2021-01-01 onward.

- **Adopt the funnel** only if its per-date cohort return beats BOTH of
  these, each with Newey-West t > 2 and an equity max drawdown no worse
  than the comparator's:
  - `scanner_top15`: a replay of today's scanner;
  - `random_quality_gate`: random picks of the same size from the same
    quality-gated pool.
- **Keep the recovery lane** only if its picks beat random gated picks
  with t > 2. Otherwise kill it.
- **If it fails,** record that in ADR-0007. Retuning thresholds against
  the result would recreate the overfit this work exists to remove. Any
  change needs a new pre-registration and a fresh out-of-sample window.

## What was decided (Dane's answers)

1. **Mandate:** momentum-first, with a recovery lane capped at 5 slots.
2. **Order:** build the backtest before any live scoring change. The two
   indicator bugs were fixed first.
3. **Schema:** approved both tables, `scan_picks` and
   `fundamentals_snapshots`.
4. **FMP plan check:** approved. It is blocked in the cloud environment,
   so it still needs to run.

## What was built (commits on the branch)

| Commit | What |
|---|---|
| `8b2ce16` | Fixed two recovery-metric bugs. `retests_held` counted the bars right after a fresh low as a retest, a near-free +10. `post_low_trend` judged on a 1-4 bar trailing block. ESTC fixture 45 → 35, ADBE −40 → −50; ordering unchanged; the winner threshold in the test was relaxed from ≥ 40 to ≥ 30. |
| `c95d572` | `app/services/funnel.py`: quality gate, A/B/C factors with D as tie-breaker, recovery lane, risk screen, tiers, trade plan and thesis. Pure and point-in-time. `_score_opportunity` became a staticmethod. |
| `403072a` | `scan_picks`: each completed scan writes its top 20, immutable. `fundamentals_snapshots`: raw statements cache plus `refresh_fundamentals()`. Hand-written migration `e1f2a3b4c5d6`. `FMPClient.get_quarterly_statements`. |
| `f24cb51` | `scripts/backtest/`: `data.py` (FMP fetch + cache, point-in-time membership), `engine.py` (replay + variants), `metrics.py` (Newey-West, equity curve, decision), `report.py`, `run.py` (CLI), `synthetic.py`. Splits `run_funnel` into `prepare_cross_section` + `select_picks`. |
| `f9f5101` | Guard for the adjusted-bars fallback, ADR-0007, TODO rotation. |
| `939c837` | TODO links to #109. |

**Tests:**
- `tests/test_services/test_funnel.py` and
  `tests/test_scripts/test_backtest.py` both include tests proving that
  scrambling future data can't change picks. Both were mutation-checked:
  they fail when future data is leaked in.
- Persistence tests are in `tests/test_services/test_scan_history.py`.

## What is deliberately NOT done

- **The funnel is not live.** The scanner ranking is unchanged apart from
  the two bug fixes.
- **No refresh job:** nothing calls `refresh_fundamentals()` on a schedule
  or route. Adding one is a Cloud Scheduler change, so ask first, and only
  after an ADOPT verdict.
- **No estimate-revisions factor:** FMP has no point-in-time revision
  history. One option is to start snapshotting estimates weekly ourselves.
- **No frontend changes.**

## The audit, condensed (only ever delivered in chat)

**Bottom line:** no edge has been demonstrated.
- The current scanner is a price-only mean-reversion score.
- Its weights (30/20/15/10/5) were fitted to 5 historical reference setups. `test_scanner_scoring.py` turns those
  outcomes into acceptance tests.

**Flaws found:**
- **≥ 20% drawdown as the entry ticket** bets against intermediate
  momentum and against proximity to the 52-week high, two of the
  best-documented 3–12 month effects.
- **The two tracks score on different scales.** Recovery maxes at 90 and
  pullback at 30 (+10 market cap on either), so drawdown names always top
  the list.
- **The uptrend track barely fires:** `ma_slope > 2` means the 50-DMA must
  rise 2% in 10 bars, roughly 50%+ a year. Healthy leaders score 0.
- **The market-cap bonus** (+10 for > $50B, +5 for > $10B) is a liquidity
  preference, not alpha.
- **Price ≥ $20 and volume > 10k shares** is not a quality filter.
  Nothing in the scanner looks at fundamentals.
- **No relative strength, no sector caps, no regime awareness.** It ranks
  all ~900 names instead of producing a short list.
- **Survivorship bias:** the universe is today's index members, hard-coded.
- **Scans were not persisted,** so live picks had no track record. Fixed
  by `scan_picks`.
- **Recovery swing lows inside the last 7 bars are unconfirmed** and can
  change later. The plateau code already fixed this; the recovery code has
  not. Still open and unfixed: it's a design choice, flag it before
  changing.

## Gotchas

- **Never apply an autogenerated Alembic migration blind.** It proposes
  dropping `accounts`, `holdings`, `trade_campaigns` and others, because
  their models aren't imported in `app/models/__init__.py`. Hand-write
  migrations until that's fixed.
- **Running tests locally:** point `NEON_DATABASE_URL` at a throwaway
  Postgres, with `ENVIRONMENT=test` and a dummy `FMP_API_KEY`. `conftest`
  refuses to run against prod.
- **Cloud agent sessions** need `financialmodelingprep.com` on the
  environment's allowed domains and `FMP_API_KEY` set as an environment
  variable before they can run the backtest.
