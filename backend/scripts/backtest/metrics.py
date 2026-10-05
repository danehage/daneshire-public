"""
Statistics for backtest output. Pure functions over the engine's rows.

Two levels, kept separate on purpose:
- pick level: every pick is one observation (hit rates, win/loss, MAE);
- strategy level: one cohort per rebalance date, cohort return = mean of
  its picks, or 0 (cash) when the variant picked nothing. Significance is
  measured here, with Newey-West errors because 63-session holds started
  weekly overlap ~13 deep.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd


def newey_west_t(x: np.ndarray, lags: int) -> tuple[float, float]:
    """(mean, t-stat) with Bartlett-kernel HAC standard error."""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return (float(x.mean()) if n else float("nan")), float("nan")
    mu = x.mean()
    e = x - mu
    var = e @ e / n
    for l in range(1, min(lags, n - 1) + 1):
        w = 1 - l / (lags + 1)
        var += 2 * w * (e[l:] @ e[:-l]) / n
    se = math.sqrt(var / n) if var > 0 else float("nan")
    return float(mu), float(mu / se) if se and se == se else float("nan")


def cohort_series(rows: pd.DataFrame, variant: str, dates: list[date]) -> pd.DataFrame:
    """Per-date cohort return and excess (cash when no picks)."""
    spy = rows[rows["variant"] == "spy"].set_index("date")["ret"]
    v = rows[rows["variant"] == variant]
    g = v.groupby("date")["ret"].mean() if len(v) else pd.Series(dtype=float)
    out = pd.DataFrame(index=pd.Index(dates, name="date"))
    out["spy"] = spy.reindex(out.index)
    out["ret"] = g.reindex(out.index).fillna(0.0)
    out["n"] = (v.groupby("date").size() if len(v) else pd.Series(dtype=int)).reindex(out.index).fillna(0)
    out["excess"] = out["ret"] - out["spy"]
    return out


def equity_curve(rows: pd.DataFrame, variant: str, dates: list[date], spy_dates: pd.DatetimeIndex, horizon: int) -> pd.Series:
    """Overlapping-cohort portfolio: each day, capital split evenly across
    the cohorts currently held; a cohort's day-k return is the mean of its
    picks' day-k returns (cash cohorts earn 0)."""
    v = rows[rows["variant"] == variant]
    by_date = {d: np.vstack(g["daily"].to_numpy()) for d, g in v.groupby("date")} if len(v) else {}
    cal = pd.DatetimeIndex(spy_dates)
    pos = cal.searchsorted(pd.to_datetime(dates), side="right")
    n = len(cal)
    tot, cnt = np.zeros(n), np.zeros(n)
    for d, p in zip(dates, pos):
        if p >= n:
            continue
        span = min(horizon, n - p)
        if d in by_date:
            r = np.nanmean(by_date[d], axis=0)[:span]
        else:
            r = np.zeros(span)
        tot[p : p + span] += r
        cnt[p : p + span] += 1
    active = cnt > 0
    daily = np.where(active, tot / np.where(active, cnt, 1), 0.0)
    first = int(np.argmax(active)) if active.any() else 0
    last = n - int(np.argmax(active[::-1])) if active.any() else 0
    return pd.Series(np.cumprod(1 + daily[first:last]), index=cal[first:last])


def max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return float("nan")
    return float((curve / curve.cummax() - 1).min())


def cagr(curve: pd.Series) -> float:
    if len(curve) < 2:
        return float("nan")
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    return float(curve.iloc[-1] ** (1 / years) - 1) if years > 0 else float("nan")


def turnover(rows: pd.DataFrame, variant: str, dates: list[date]) -> float:
    v = rows[(rows["variant"] == variant)]
    sets = [set(v.loc[v["date"] == d, "ticker"]) for d in dates]
    ch = [len(b - a) / len(b) for a, b in zip(sets, sets[1:]) if b]
    return float(np.mean(ch)) if ch else float("nan")


def summarize(rows: pd.DataFrame, variant: str, dates: list[date], spy_dates, horizon: int, lags: int) -> dict:
    v = rows[(rows["variant"] == variant) & rows["date"].isin(dates)]
    coh = cohort_series(rows, variant, dates)
    mu, t = newey_west_t(coh["excess"].to_numpy(), lags)
    curve = equity_curve(rows, variant, dates, spy_dates, horizon)
    wins, losses = v[v["ret"] > 0]["ret"], v[v["ret"] <= 0]["ret"]
    return {
        "variant": variant,
        "picks": int(len(v)),
        "dates": len(dates),
        "picks_per_date": float(len(v) / len(dates)) if dates else float("nan"),
        "empty_dates": int((coh["n"] == 0).sum()),
        "hit10": float(v["hit10"].mean()) if len(v) else float("nan"),
        "touched10_first": float(v["touched10_first"].mean()) if len(v) else float("nan"),
        "mean_ret": float(v["ret"].mean()) if len(v) else float("nan"),
        "median_ret": float(v["ret"].median()) if len(v) else float("nan"),
        "mean_excess_pick": float(v["excess"].mean()) if len(v) else float("nan"),
        "avg_win": float(wins.mean()) if len(wins) else float("nan"),
        "avg_loss": float(losses.mean()) if len(losses) else float("nan"),
        "mean_mae": float(v["mae"].mean()) if len(v) else float("nan"),
        "managed_mean_ret": float(v["managed_ret"].mean()) if len(v) else float("nan"),
        "truncated": int(v["truncated"].sum()) if len(v) else 0,
        "cohort_excess": mu,
        "cohort_excess_t": t,
        "equity_max_dd": max_drawdown(curve),
        "equity_cagr": cagr(curve),
        "turnover": turnover(rows, variant, dates),
    }


def paired_test(rows: pd.DataFrame, a: str, b: str, dates: list[date], lags: int) -> tuple[float, float]:
    """Mean and NW t of (cohort return a - cohort return b) per date."""
    ca, cb = cohort_series(rows, a, dates), cohort_series(rows, b, dates)
    return newey_west_t((ca["ret"] - cb["ret"]).to_numpy(), lags)


def lane_vs(rows: pd.DataFrame, lane: str, comparator: str, dates: list[date], lags: int) -> tuple[float, float, int]:
    """Recovery-lane picks vs comparator, on dates where the lane picked."""
    f = rows[(rows["variant"] == "funnel") & (rows["lane"] == lane) & rows["date"].isin(dates)]
    if f.empty:
        return float("nan"), float("nan"), 0
    lane_ret = f.groupby("date")["ret"].mean()
    comp = rows[(rows["variant"] == comparator) & rows["date"].isin(lane_ret.index)].groupby("date")["ret"].mean()
    diff = (lane_ret - comp.reindex(lane_ret.index)).dropna()
    mu, t = newey_west_t(diff.to_numpy(), lags)
    return mu, t, int(len(f))


def decision(rows: pd.DataFrame, summaries: dict, oos_dates: list[date], lags: int) -> dict:
    """The pre-registered rule (ADR-0007), evaluated on out-of-sample only:
    the funnel ships only if its cohort return beats BOTH the current scanner
    and random quality-gate picks with NW t > 2, without a worse equity
    max drawdown. The recovery lane survives only if its picks beat random
    quality-gate picks with t > 2."""
    out = {"comparisons": {}, "adopt_funnel": True}
    for comp in ("scanner_top15", "random_quality_gate"):
        if comp not in summaries:
            out["comparisons"][comp] = {"available": False}
            out["adopt_funnel"] = False
            continue
        mu, t = paired_test(rows, "funnel", comp, oos_dates, lags)
        dd_ok = summaries["funnel"]["equity_max_dd"] >= summaries[comp]["equity_max_dd"]
        passed = bool(mu > 0 and t == t and t > 2 and dd_ok)
        out["comparisons"][comp] = {"mean_diff": mu, "t": t, "dd_ok": bool(dd_ok), "passed": passed}
        out["adopt_funnel"] &= passed
    mu, t, n = lane_vs(rows, "recovery", "random_quality_gate", oos_dates, lags)
    out["recovery_lane"] = {"mean_diff": mu, "t": t, "picks": n, "keep": bool(n and t == t and t > 2 and mu > 0)}
    return out
