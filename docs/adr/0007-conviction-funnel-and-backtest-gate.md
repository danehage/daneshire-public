# Conviction funnel, gated by a pre-registered backtest

> Status: accepted (2026-10-03) for building and backtesting; the funnel is NOT live. Adoption into the scanner ranking waits on the backtest verdict below.

The scanner's ranking (`StockScanner._score_opportunity`) is a price-only mean-reversion score whose weights were tuned on five hand-picked trades with known outcomes (ESTC, EOG, APD, ADBE, AMC — `tests/test_services/test_scanner_scoring.py`). It has no fundamentals, no market-relative strength and no regime awareness, and it ranks all ~900 names instead of producing a short list. The goal is a curated 10–20 names with a 3-month horizon (+10–25%), excluding low-quality companies.

## Decision

1. **Funnel** (`backend/app/services/funnel.py`): a pure, point-in-time, four-stage pipeline.
   - *Quality gate* (hard): market cap ≥ $2B, price ≥ $10, 20-day dollar volume ≥ $20M; TTM FCF > 0 (Financials/REITs: TTM net income > 0); TTM revenue YoY ≥ 0 and not down YoY two quarters running; net debt / EBITDA ≤ 3.5× (not Financials/REITs/Utilities); diluted shares YoY ≤ +5%; Altman Z > 1.8 (not Financials/REITs/Utilities). Missing data fails.
   - *Setup score*: equal-weight percentile ranks of A (6-month return skipping 2 weeks, half vs universe, half vs sector median), B (price / 52-week high, behind a trend template: price > 50-DMA > 200-DMA, 200-DMA rising) and C (last report within 90 days: EPS surprise, revenue surprise, drift since the pre-report close). D (20d/120d volatility ratio + 50-day up/down volume) is a tie-breaker only.
   - *Recovery lane*, at most 5 slots: quality names ≥ 20% off the 52-week high that beat their last report and are back above the 50-DMA, ranked by the existing recovery-track points.
   - *Risk screen*: no report in the next 14 calendar days, ATR ≤ 5% of price, ≤ 3 names per sector, pairwise 60-day return correlation ≤ 0.7. Regime: SPY > 200-DMA AND > 50% of the universe above its own 200-DMA → up to 15 names (tiers A+B); otherwise up to 6, A tier only.
   - *Output*: tier (A = top decile composite, B = top quartile), one-line thesis, entry note, stop (tighter of 2×ATR or below the last confirmed swing low, risk capped at 10%), target (max of +15% and 2.5R), 63-session time stop.
2. **Every threshold was fixed before any backtest ran** and lives in `FunnelConfig`. It is not tuned against backtest output.
3. **Backtest** (`backend/scripts/backtest/`): weekly rebalance 2016 → today, point-in-time S&P 500 membership, entry next open, 63-session hold, 10 bps per side. Compared against a replay of the current scanner's top 15, random picks of the same size from the same quality-gated pool, the equal-weight universe and SPY. Ablations (each factor, gate, risk screen, recovery lane) and ±20% lookback sensitivity.
4. **Pre-registered adoption rule**, on out-of-sample dates only (2021-01-01 onward): adopt the funnel only if its per-date cohort return beats BOTH the current scanner and random gated picks with Newey-West t > 2, without a worse equity max drawdown. The recovery lane is kept only if its picks beat random gated picks with t > 2.
5. **Forward record**: every live scan writes its top 20 to `scan_picks` (immutable), so live picks accumulate an out-of-sample track record no one can fit after the fact.

## Considered options

- **Re-weight the existing score.** Rejected: any weights fitted to our own trade history repeat the n=5 overfit.
- **Many factors / ML ranking.** Rejected for now: more degrees of freedom than ~10 years of weekly cohorts can support honestly.
- **Momentum only, no recovery lane.** Kept as an ablation (`funnel_momentum_only`); the lane survives only on evidence.
- **Estimate revisions as a factor.** Deferred: FMP has no point-in-time revision history, so it can't be backtested. Revisit after snapshotting estimates ourselves for a year.

## Amendment (2026-10-05, before any real-data run)

Data-plumbing corrections, made before anyone had seen a result. No threshold or rule changed.

- **Dollar thresholds use the as-traded tape.** The price floor, the 20-day dollar volume and market cap (funnel gate and the scanner replay's Stage 1) now read unadjusted close/volume and FMP's historical market cap (`/stable/historical-market-capitalization`). Before this they read split- and dividend-adjusted bars, so a name that later split heavily (NVDA's 2016 close is ~$1 after its splits) failed the $10 / $20 floors and the $2B cap, using knowledge of its future splits. Returns still use dividend-adjusted bars. Where FMP has no unadjusted series or market cap for a symbol, the harness falls back to adjusted prices / adjusted price x diluted shares and the report counts those symbols.
- The backtest runs on Windows (spawn workers; UTF-8 output). No effect on results.
- **Membership source.** Our FMP plan returns HTTP 402 for the S&P 500 constituent endpoints, so point-in-time membership comes from the public daily file at github.com/fja05680/sp500 (MIT), pinned to commit `a2430f2` (last updated 2026-08-18; later dates reuse that snapshot). `--universe pit500public` is the default; FMP's `pit500` remains for plans that serve it. Prices and fundamentals still come from FMP.

## Known limits

- Removed S&P 500 members whose tickers FMP no longer serves are missing (residual survivorship bias; the report counts them). The public file lists renamed companies under their old tickers (e.g. FB before META); if FMP only serves the new ticker, those dates are missing too. A reused ticker could pull in another company's prices.
- Sector is today's profile sector for every date.
- The earnings blackout uses actual report dates (usually confirmed 2–4 weeks ahead).
- FMP statements are most likely restated figures, not as first filed. Filing dates gate *when* a quarter is known, but its values may include later restatements (mild look-ahead in the quality gate).
- FMP split-adjusts historical share counts (verified 2026-10-05: AAPL 2011 diluted shares = 26.3B), so the market-cap fallback multiplies them by the adjusted price. Dividend adjustment makes that fallback slightly low for high-yielders; it applies only to symbols with no historical market cap.
- `current900` mode replays today's S&P 500+400 and is labelled survivorship-biased in the report; use it only if the point-in-time endpoints are unavailable on our plan.

## When to revisit

- After the first real backtest run: adopt, adjust (only with a new pre-registration and a fresh out-of-sample window), or abandon.
- After ~6 months of `scan_picks`: compare live picks' realized 63-session returns to the backtest's expectation.
