"""
Deterministic synthetic market for tests and the ``--synthetic`` dry run.

NOT evidence of anything. Returns are random walks with a per-ticker drift
regime, so the backtest pipeline can be exercised end to end (and checked for
look-ahead) without FMP access. Any "edge" a synthetic run shows is an
artifact of how this generator was written.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

SECTORS = ["Technology", "Healthcare", "Industrials", "Energy", "Financial Services", "Utilities"]


def business_days(start: date, end: date) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


def make_bars(rng: np.random.Generator, days: pd.DatetimeIndex, start_px: float, drift_path: np.ndarray, vol: float) -> pd.DataFrame:
    rets = rng.normal(drift_path, vol)
    close = start_px * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, vol / 3, len(days)))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, vol / 2, len(days))))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, vol / 2, len(days))))
    volume = rng.integers(500_000, 5_000_000, len(days)).astype(float)
    return pd.DataFrame(
        {"date": days, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def make_statements(rng: np.random.Generator, days: pd.DatetimeIndex, quality: bool, shares: float) -> tuple[list, list, list]:
    """Quarterly statements, newest first, each with a filingDate 35 days
    after period end. ``quality`` flips FCF, growth and leverage."""
    income, balance, cash = [], [], []
    q_ends = pd.date_range(days[0] - pd.Timedelta(days=800), days[-1], freq="QE")
    rev = 1e9
    growth = 0.03 if quality else -0.03
    for qe in q_ends:
        rev *= 1 + growth + rng.normal(0, 0.01)
        filing = (qe + pd.Timedelta(days=35)).date().isoformat()
        op_inc = rev * (0.2 if quality else -0.02)
        income.append({
            "date": qe.date().isoformat(), "filingDate": filing, "revenue": rev,
            "netIncome": op_inc * 0.8, "operatingIncome": op_inc, "ebitda": op_inc + rev * 0.05,
            "weightedAverageShsOutDil": shares, "eps": op_inc * 0.8 / shares,
        })
        balance.append({
            "date": qe.date().isoformat(), "filingDate": filing,
            "totalAssets": rev * 6, "totalLiabilities": rev * (2 if quality else 5.5),
            "totalCurrentAssets": rev * 2, "totalCurrentLiabilities": rev * 1,
            "retainedEarnings": rev * (2 if quality else -1),
            "totalDebt": rev * (1 if quality else 4), "cashAndCashEquivalents": rev * 0.5,
        })
        cash.append({
            "date": qe.date().isoformat(), "filingDate": filing,
            "freeCashFlow": rev * (0.12 if quality else -0.05),
            "operatingCashFlow": rev * 0.2, "capitalExpenditure": -rev * 0.08,
            "depreciationAndAmortization": rev * 0.05,
        })
    return income[::-1], balance[::-1], cash[::-1]


def make_earnings(rng: np.random.Generator, days: pd.DatetimeIndex, bias: float) -> list[dict]:
    out = []
    for qe in pd.date_range(days[0] - pd.Timedelta(days=400), days[-1] + pd.Timedelta(days=120), freq="QE"):
        rd = (qe + pd.Timedelta(days=30)).date()
        est = 1.0 + rng.normal(0, 0.1)
        act = est * (1 + bias + rng.normal(0, 0.08))
        out.append({
            "date": rd.isoformat(), "epsEstimated": est, "epsActual": act,
            "revenueEstimated": 1e9, "revenueActual": 1e9 * (1 + bias / 2 + rng.normal(0, 0.02)),
        })
    return out[::-1]


def make_market(n_tickers: int = 60, start: date = date(2015, 1, 1), end: date = date(2020, 12, 31), seed: int = 7) -> dict:
    """{'tickers': {sym: {...}}, 'spy': DataFrame, 'membership': [(date, set)]}"""
    rng = np.random.default_rng(seed)
    days = business_days(start, end)
    tickers = {}
    for i in range(n_tickers):
        sym = f"T{i:03d}"
        quality = i % 4 != 0
        # Drift switches regime every ~120 bars; persistence gives momentum
        # something to find in synthetic data only.
        drift = np.repeat(rng.normal(0.0004, 0.0012, len(days) // 120 + 1), 120)[: len(days)]
        bars = make_bars(rng, days, rng.uniform(20, 300), drift, rng.uniform(0.01, 0.025))
        inc, bal, cf = make_statements(rng, days, quality, shares=rng.uniform(5e7, 1e9))
        tickers[sym] = {
            "sector": SECTORS[i % len(SECTORS)],
            "bars": bars,
            "income": inc,
            "balance": bal,
            "cashflow": cf,
            "earnings": make_earnings(rng, days, 0.05 if quality else -0.03),
        }
    spy_drift = np.full(len(days), 0.0003)
    spy = make_bars(rng, days, 200.0, spy_drift, 0.01)
    # Membership changes: one name leaves the "index" halfway through, to
    # exercise point-in-time universe handling.
    all_syms = set(tickers)
    mid = days[len(days) // 2].date()
    membership = [(start, all_syms), (mid, all_syms - {"T001"})]
    return {"tickers": tickers, "spy": spy, "membership": membership, "days": days}
