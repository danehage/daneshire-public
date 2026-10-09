# Funnel backtest — S&P 500, point-in-time (fja05680/sp500 @ a2430f2), 2016-01-08 → 2026-07-07

## Verdict (pre-registered rule, out-of-sample 2021-01-01 onward): **DO NOT ADOPT**

- vs `scanner_top15`: mean cohort difference +1.19%, NW t 1.19, drawdown no worse: yes → **fail**
- vs `random_quality_gate`: mean cohort difference -0.39%, NW t -0.53, drawdown no worse: no → **fail**
- Recovery lane vs random quality-gate picks: 680 picks, mean diff -2.16%, t -1.83 → **kill**

Rule: adopt only if the funnel's per-date cohort return beats BOTH the current scanner and random picks from the same quality-gated pool with Newey-West t > 2, without a worse equity max drawdown. Thresholds were fixed before this run; nothing was tuned on it.

## Setup

- Universe: S&P 500, point-in-time (fja05680/sp500 @ a2430f2); 745 symbols ever members, 735 with price data (10 missing: residual survivorship bias), 11 without statements (fail the quality gate).
- Dollar thresholds use unadjusted prices and historical market cap. Fell back to adjusted prices for 0 symbols and to adjusted price x split-adjusted shares for 22; those can be wrongly screened out ahead of a later split.
- Rebalance: weekly (last session of each week), 549 dates (2016-01-08 → 2026-07-07); in-sample 261, out-of-sample 288.
- Signal at the rebalance close, entry next open, 63-session hold, 10 bps per side. Managed = funnel stop/target applied on daily bars (stop assumed first on a both-hit day).
- Statistics: cohort = one rebalance date; empty cohorts hold cash. Newey-West lags = 13.
- Truncated holds (data ended mid-hold, exited at last close): 42 funnel picks.

## Out-of-sample — main comparison

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 3308 | 11.5 | 3 | 26.8% | 32.5% | +1.9% | +1.2% | +12.8% | -10.8% | -10.8% | +0.4% | -1.2% | -1.35 | -22.0% | +7.7% | 48.7% |
| scanner_top15 | 4319 | 15.0 | 0 | 28.2% | 37.2% | +1.0% | +0.1% | +15.3% | -13.4% | -13.4% | +0.1% | -2.4% | -3.13 | -29.7% | +4.3% | 50.4% |
| random_quality_gate | 66168 | 229.8 | 3 | 26.7% | 32.6% | +2.3% | +1.8% | +12.2% | -10.0% | -9.9% | +0.6% | -0.8% | -1.68 | -22.0% | +9.9% | 44.5% |
| universe_equal_weight | 144436 | 501.5 | 0 | 27.5% | 33.9% | +2.5% | +1.8% | +12.7% | -10.4% | -10.4% | +0.6% | -1.0% | -1.88 | -20.7% | +9.8% | 0.1% |
| spy | 288 | 1.0 | 0 | 13.5% | 25.0% | +3.4% | +4.4% | +6.5% | -5.7% | -5.2% | +3.4% | +0.0% | – | -24.3% | +13.8% | 0.0% |

## In-sample — main comparison

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 3126 | 12.0 | 5 | 25.3% | 30.9% | +1.7% | +2.3% | +10.7% | -11.0% | -10.2% | +0.5% | -1.3% | -1.75 | -38.2% | +10.4% | 47.5% |
| scanner_top15 | 3915 | 15.0 | 0 | 34.7% | 37.4% | +2.4% | +2.8% | +15.4% | -14.4% | -13.1% | +1.0% | -1.6% | -1.19 | -51.6% | +9.0% | 46.4% |
| random_quality_gate | 62519 | 239.5 | 5 | 25.0% | 30.5% | +2.0% | +2.7% | +10.4% | -10.1% | -9.5% | +0.8% | -1.0% | -1.94 | -36.8% | +13.4% | 37.3% |
| universe_equal_weight | 126401 | 484.3 | 0 | 29.5% | 33.7% | +3.7% | +3.7% | +12.4% | -10.5% | -9.9% | +1.3% | -0.3% | -0.63 | -38.9% | +16.0% | 0.1% |
| spy | 261 | 1.0 | 0 | 13.0% | 21.1% | +4.0% | +4.7% | +6.6% | -6.5% | -4.9% | +4.0% | +0.0% | – | -32.7% | +16.4% | 0.0% |

## Full period — main comparison

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 6434 | 11.7 | 8 | 26.1% | 31.7% | +1.8% | +1.7% | +11.8% | -10.9% | -10.5% | +0.4% | -1.3% | -2.13 | -38.2% | +8.8% | 48.1% |
| scanner_top15 | 8234 | 15.0 | 0 | 31.3% | 37.3% | +1.7% | +1.3% | +15.3% | -13.8% | -13.3% | +0.5% | -2.0% | -2.61 | -51.6% | +4.9% | 48.5% |
| random_quality_gate | 128687 | 234.4 | 8 | 25.9% | 31.6% | +2.1% | +2.2% | +11.3% | -10.1% | -9.7% | +0.7% | -0.9% | -2.55 | -36.8% | +11.1% | 41.0% |
| universe_equal_weight | 270837 | 493.3 | 0 | 28.4% | 33.8% | +3.1% | +2.8% | +12.5% | -10.4% | -10.2% | +0.9% | -0.7% | -1.81 | -38.9% | +11.9% | 0.1% |
| spy | 549 | 1.0 | 0 | 13.3% | 23.1% | +3.7% | +4.5% | +6.5% | -6.0% | -5.1% | +3.7% | +0.0% | – | -32.7% | +14.7% | 0.0% |

## Out-of-sample — ablations and sensitivity

Each row removes one piece of the funnel or scales every lookback. A piece that doesn't hurt when removed isn't earning its place; results that flip under ±20% lookbacks are fragile.

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 3308 | 11.5 | 3 | 26.8% | 32.5% | +1.9% | +1.2% | +12.8% | -10.8% | -10.8% | +0.4% | -1.2% | -1.35 | -22.0% | +7.7% | 48.7% |
| funnel_lookback_x0.8 | 3276 | 11.4 | 2 | 26.5% | 31.2% | +1.8% | +0.7% | +12.9% | -10.5% | -10.8% | +0.2% | -1.2% | -1.33 | -21.0% | +7.5% | 49.6% |
| funnel_lookback_x1.2 | 3392 | 11.8 | 4 | 27.2% | 32.5% | +2.0% | +1.3% | +12.8% | -10.7% | -10.7% | +0.5% | -1.2% | -1.59 | -22.8% | +7.7% | 47.8% |
| funnel_momentum_only | 3124 | 10.8 | 3 | 25.8% | 31.7% | +2.4% | +1.6% | +12.1% | -9.7% | -10.1% | +0.4% | -0.7% | -0.77 | -20.7% | +9.5% | 46.1% |
| funnel_no_quality_gate | 3542 | 12.3 | 1 | 25.9% | 32.7% | +1.3% | +0.6% | +13.3% | -11.6% | -11.5% | +0.2% | -2.1% | -2.29 | -20.6% | +4.6% | 53.0% |
| funnel_no_risk_screen | 4207 | 14.6 | 0 | 27.2% | 33.0% | +2.3% | +1.1% | +14.1% | -11.0% | -11.4% | +0.3% | -1.1% | -1.26 | -26.1% | +7.7% | 43.8% |
| funnel_without_A | 3295 | 11.4 | 4 | 25.2% | 31.6% | +1.5% | +1.1% | +11.9% | -10.6% | -10.3% | +0.5% | -1.9% | -2.38 | -23.0% | +4.0% | 53.3% |
| funnel_without_B | 3301 | 11.5 | 4 | 28.2% | 33.2% | +2.4% | +1.1% | +14.2% | -11.0% | -11.3% | +0.3% | -0.6% | -0.79 | -22.4% | +10.0% | 41.5% |
| funnel_without_C | 3312 | 11.5 | 5 | 25.7% | 31.9% | +1.9% | +1.3% | +12.4% | -10.7% | -10.5% | +0.3% | -1.3% | -1.46 | -19.9% | +7.3% | 56.5% |

## By regime

### Risk-on dates (428)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 6015 | 14.1 | 0 | 25.6% | 31.5% | +1.6% | +1.6% | +11.6% | -11.0% | -10.6% | +0.4% | -1.1% | -2.10 | -41.6% | +7.4% | 48.2% |
| scanner_top15 | 6419 | 15.0 | 0 | 29.2% | 35.3% | +0.6% | +0.3% | +14.8% | -14.1% | -13.5% | +0.2% | -2.3% | -2.49 | -56.2% | +0.4% | 47.0% |
| random_quality_gate | 120310 | 281.1 | 0 | 25.4% | 31.0% | +2.0% | +2.1% | +11.2% | -10.1% | -9.7% | +0.7% | -0.8% | -2.86 | -37.9% | +9.3% | 31.6% |
| universe_equal_weight | 210816 | 492.6 | 0 | 26.1% | 31.4% | +2.1% | +2.1% | +11.8% | -10.6% | -10.1% | +0.6% | -0.8% | -1.92 | -39.8% | +8.7% | 0.1% |
| spy | 428 | 1.0 | 0 | 6.8% | 13.3% | +2.9% | +4.1% | +5.5% | -6.2% | -4.7% | +2.9% | +0.0% | – | -33.6% | +12.7% | 0.0% |

### Risk-off dates (121)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 419 | 3.5 | 8 | 32.5% | 34.4% | +5.0% | +4.0% | +13.2% | -8.1% | -9.0% | +1.0% | -1.7% | -1.00 | -23.9% | +6.4% | 55.9% |
| scanner_top15 | 1815 | 15.0 | 0 | 38.8% | 44.5% | +5.8% | +5.3% | +16.7% | -12.5% | -12.4% | +1.7% | -0.8% | -0.88 | -31.9% | +4.3% | 61.6% |
| random_quality_gate | 8377 | 69.2 | 8 | 32.5% | 39.3% | +4.7% | +4.3% | +13.1% | -9.6% | -10.2% | +1.2% | -1.4% | -1.28 | -26.0% | +5.4% | 77.7% |
| universe_equal_weight | 60021 | 496.0 | 0 | 36.7% | 42.5% | +6.3% | +5.3% | +14.8% | -9.9% | -10.4% | +2.1% | -0.3% | -0.45 | -29.3% | +6.7% | 0.4% |
| spy | 121 | 1.0 | 0 | 36.4% | 57.9% | +6.6% | +7.5% | +10.4% | -5.4% | -6.1% | +6.6% | +0.0% | – | -22.6% | +8.6% | 0.0% |

### 2018 Q4 selloff (13 signal dates)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 72 | 5.5 | 0 | 16.7% | 4.2% | +2.0% | -0.3% | +13.4% | -8.7% | -14.3% | -3.6% | +1.4% | 1.72 | -19.5% | +14.7% | 37.6% |
| scanner_top15 | 195 | 15.0 | 0 | 31.3% | 24.6% | +1.0% | +1.6% | +13.3% | -13.5% | -17.7% | -1.6% | -1.7% | -1.41 | -24.1% | -8.0% | 60.0% |
| random_quality_gate | 1440 | 110.8 | 0 | 22.7% | 12.6% | +0.9% | +1.5% | +9.9% | -9.9% | -15.2% | -3.2% | +0.6% | 3.93 | -19.7% | -2.0% | 71.9% |
| universe_equal_weight | 6383 | 491.0 | 0 | 28.5% | 23.4% | +3.5% | +3.5% | +11.5% | -9.4% | -13.6% | -1.3% | +0.7% | 10.38 | -18.6% | +1.2% | 0.1% |
| spy | 13 | 1.0 | 0 | 15.4% | 23.1% | +2.8% | +0.5% | +8.1% | -3.5% | -12.0% | +2.8% | +0.0% | – | -18.7% | -1.3% | 0.0% |

### 2020 COVID crash (10 signal dates)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 21 | 2.1 | 5 | 28.6% | 4.8% | +2.3% | +6.0% | +15.2% | -14.8% | -29.7% | -3.8% | -6.9% | -2.75 | -27.7% | -9.9% | 50.0% |
| scanner_top15 | 150 | 15.0 | 0 | 63.3% | 54.0% | +12.6% | +16.1% | +22.9% | -16.6% | -17.5% | +5.5% | -2.9% | -2.05 | -39.0% | -25.0% | 72.6% |
| random_quality_gate | 420 | 42.0 | 5 | 20.2% | 6.0% | -5.5% | -5.2% | +16.8% | -17.9% | -35.4% | -4.5% | -12.0% | -6.07 | -30.8% | -28.5% | 69.1% |
| universe_equal_weight | 4960 | 496.0 | 0 | 56.4% | 48.4% | +14.3% | +13.4% | +23.9% | -14.0% | -18.1% | +3.6% | -1.2% | -1.31 | -33.8% | -3.2% | 0.1% |
| spy | 10 | 1.0 | 0 | 80.0% | 80.0% | +15.6% | +15.7% | +18.2% | -8.4% | -11.0% | +15.6% | +0.0% | – | -26.8% | +11.3% | 0.0% |

### 2022 bear market (43 signal dates)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 208 | 4.8 | 1 | 20.7% | 21.6% | -1.7% | -1.4% | +11.2% | -12.5% | -13.4% | -2.1% | +3.6% | 1.34 | -18.9% | +5.7% | 49.8% |
| scanner_top15 | 645 | 15.0 | 0 | 21.2% | 29.1% | -3.6% | -4.2% | +12.9% | -15.0% | -17.6% | -2.9% | -0.5% | -0.20 | -30.4% | -10.9% | 61.0% |
| random_quality_gate | 4157 | 96.7 | 1 | 14.1% | 21.5% | -4.6% | -5.4% | +10.4% | -12.7% | -15.3% | -3.1% | +1.8% | 2.18 | -19.5% | -2.3% | 75.1% |
| universe_equal_weight | 21579 | 501.8 | 0 | 20.7% | 30.0% | -1.5% | -1.7% | +11.4% | -12.1% | -13.8% | -1.4% | +1.6% | 2.20 | -19.7% | -2.8% | 0.1% |
| spy | 43 | 1.0 | 0 | 0.0% | 27.9% | -3.1% | -4.8% | +5.2% | -7.1% | -10.4% | -3.1% | +0.0% | – | -22.6% | -9.7% | 0.0% |

### 2025 Feb-Apr drawdown (10 signal dates)

| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| funnel | 67 | 6.7 | 0 | 25.4% | 13.4% | +1.7% | +0.3% | +9.5% | -7.8% | -15.0% | -2.7% | -8.3% | -2.44 | -13.4% | -28.9% | 58.9% |
| scanner_top15 | 150 | 15.0 | 0 | 29.3% | 31.3% | +2.9% | +0.4% | +17.1% | -12.0% | -14.5% | +0.2% | -7.4% | -10.76 | -15.7% | -2.5% | 61.5% |
| random_quality_gate | 1340 | 134.0 | 0 | 22.7% | 17.2% | +3.2% | +0.6% | +12.5% | -7.7% | -15.6% | -2.5% | -2.3% | -3.83 | -13.9% | +15.3% | 65.0% |
| universe_equal_weight | 5030 | 503.0 | 0 | 33.2% | 31.3% | +6.9% | +3.4% | +16.2% | -8.7% | -12.5% | +0.9% | -3.5% | -6.29 | -14.4% | +10.4% | 0.1% |
| spy | 10 | 1.0 | 0 | 50.0% | 50.0% | +10.3% | +9.4% | +13.4% | -2.0% | -10.8% | +10.3% | +0.0% | – | -16.1% | +15.9% | 0.0% |

## Funnel stage counts (median per date)

| universe | with_history | quality | momentum_eligible | recovery_eligible | picks |
|---|---|---|---|---|---|
| 502 | 500 | 258 | 99 | 6 | 15 |

## Caveats

- Next-earnings dates for the blackout use the actual report date; companies usually confirm 2-4 weeks ahead, so inside a 14-day window this is close to what was knowable.
- Sector is today's FMP profile sector for every date.
- Removed index members whose tickers FMP no longer serves are missing (count above).
- Estimate revisions are not tested: FMP has no point-in-time revision history.
