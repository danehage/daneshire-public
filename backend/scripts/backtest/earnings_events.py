"""
Earnings event study: what happens after a company beats and the stock
drops anyway? Exploration tool, run from backend/ on the cached FMP data:

    python -m scripts.backtest.earnings_events --cache-only --cache-dir <path>

Every S&P 500 report (point-in-time membership) becomes one event:
- reaction: close the session before the report date -> close the session
  after it, minus SPY over the same window. FMP doesn't say whether a report
  came before the open or after the close; this two-session window covers
  both.
- entry: next open after the reaction window; hold N sessions; 10 bps per
  side. Excess = return minus SPY bought and sold on the same days.
- quality: the ADR-0007 funnel quality gate, evaluated at the reaction
  window's close with statements filed before that day.

Events are clustered by entry week (many reports land in the same weeks),
and t-stats are Newey-West over the weekly means, lagged for the hold's
overlap.

SEALED: 2021-01-01 onward is the out-of-sample window for a future
pre-registered test. This tool refuses to load any data after 2020-12-31
unless --sealed-run is passed, and that should happen once, after the rules
are locked in an ADR.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.services.funnel import (
    FunnelConfig,
    bars_needed,
    compute_quality_metrics,
    compute_technicals,
    passes_quality_gate,
)
from scripts.backtest import data as D
from scripts.backtest.engine import _Arrays
from scripts.backtest.metrics import newey_west_t

EXPLORE_END = date(2020, 12, 31)
OUT = Path(__file__).resolve().parent / "output" / "earnings"
HORIZONS = (5, 21, 63, 126)
PRIMARY_H = 63
COST_BPS = 10.0
# Excess reaction buckets, in percent.
BUCKETS = [(-math.inf, -10), (-10, -5), (-5, 0), (0, 5), (5, math.inf)]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _spy_idx(spy: _Arrays, day: np.datetime64) -> Optional[int]:
    i = int(np.searchsorted(spy.dates, day, side="left"))
    return i if i < len(spy.dates) and spy.dates[i] == day else None


def build_events(ds: D.Dataset, start: date, end: date, cfg: FunnelConfig = FunnelConfig()) -> pd.DataFrame:
    spy = _Arrays(ds.spy)
    cost = COST_BPS / 1e4
    n_bars = bars_needed(cfg)
    rows = []
    for sym, t in ds.tickers.items():
        a = _Arrays(t["bars"])
        n = len(a.dates)
        seen = set()
        # A renamed company's rows already come from both tickers
        # (data.merged_earnings); membership is checked per report date below.
        for e in sorted(t["earnings"], key=lambda r: str(r.get("lastUpdated") or ""), reverse=True):
            d = D._d(e.get("date"))
            if d is None or d < start or d > end or d in seen:
                continue
            seen.add(d)
            eps_a, eps_e = _num(e.get("epsActual")), _num(e.get("epsEstimated"))
            rev_a, rev_e = _num(e.get("revenueActual")), _num(e.get("revenueEstimated"))
            if eps_a is None and rev_a is None:
                continue  # not reported yet
            if sym not in ds.members_at(d):
                continue
            i_rep = int(np.searchsorted(a.dates, np.datetime64(d, "D"), side="left"))
            if i_rep == 0 or i_rep + 2 >= n:
                continue
            if (a.dates[i_rep] - np.datetime64(d, "D")).astype(int) > 4:
                continue  # price gap around the report
            i0, i1, ie = i_rep - 1, i_rep + 1, i_rep + 2
            s0, s1, se = _spy_idx(spy, a.dates[i0]), _spy_idx(spy, a.dates[i1]), _spy_idx(spy, a.dates[ie])
            if s0 is None or s1 is None or se is None:
                continue
            react = a.close[i1] / a.close[i0] - 1
            # Trend before the report, from the pre-report close.
            above_200_pre = bool(a.close[i0] > a.close[i0 - 199 : i0 + 1].mean()) if i0 >= 199 else None
            spy_react = spy.close[s1] / spy.close[s0] - 1

            eps_beat = eps_a is not None and eps_e is not None and eps_a > eps_e
            rev_beat = rev_a is not None and rev_e is not None and rev_a > rev_e
            eps_miss = eps_a is not None and eps_e is not None and eps_a < eps_e
            rev_miss = rev_a is not None and rev_e is not None and rev_a < rev_e

            row = {
                "ticker": sym,
                "sector": t["sector"],
                "report_date": d,
                "entry_date": pd.Timestamp(a.dates[ie]).date(),
                "eps_actual": eps_a,
                "eps_est": eps_e,
                "rev_actual": rev_a,
                "rev_est": rev_e,
                "eps_surprise": (eps_a - eps_e) / max(abs(eps_e), 0.01) if eps_a is not None and eps_e is not None else None,
                "rev_surprise": rev_a / rev_e - 1 if rev_a is not None and rev_e and rev_e > 0 else None,
                "eps_beat": eps_beat,
                "rev_beat": rev_beat,
                "any_beat": eps_beat or rev_beat,
                "both_beat": eps_beat and rev_beat,
                "any_miss": eps_miss or rev_miss,
                "react": react,
                "react_excess": react - spy_react,
                "above_200_pre": above_200_pre,
            }

            entry = a.open[ie]
            spy_entry = spy.open[se]
            for h in HORIZONS:
                x, sx = ie + h - 1, se + h - 1
                if entry > 0 and x < n and sx < len(spy.dates):
                    r = a.close[x] / entry - 1 - 2 * cost
                    sr = spy.close[sx] / spy_entry - 1 - 2 * cost
                    row[f"ret_{h}"], row[f"excess_{h}"] = r, r - sr
                else:
                    row[f"ret_{h}"] = row[f"excess_{h}"] = None

            # Quality gate and context, as known at the reaction window's close.
            as_of = pd.Timestamp(a.dates[i1]).date()
            tech = compute_technicals(a.frame.iloc[max(0, i1 + 1 - n_bars) : i1 + 1], cfg)
            if tech is None:
                row.update(quality=None, drawdown_pct=None, trend_ok=None, above_200=None)
            else:
                qm = compute_quality_metrics(
                    t["income"], t["balance"], t["cashflow"], as_of, tech["price"], cfg, market_cap=tech["market_cap"]
                )
                ok, _ = passes_quality_gate(qm, t["sector"], tech["raw_price"], tech["dollar_volume"], cfg)
                row.update(
                    quality=ok,
                    drawdown_pct=tech["drawdown_pct"],
                    trend_ok=tech["trend_ok"],
                    above_200=tech["above_ma_slow"],
                )
            rows.append(row)
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values(["entry_date", "ticker"]).reset_index(drop=True)
    return df


def refinements(ev: pd.DataFrame, thr: float) -> list[tuple[str, pd.Series]]:
    """R1-R3, fixed 2026-10-10 before any of them was computed.
    R1: revenue beat >= 2% (harder to manufacture than EPS) and a drop.
    R2: any beat and a drop while the stock was above its 200-day average
        the session before the report (a drop in a working stock is more
        likely temporary).
    R3: both."""
    drop = ev["react_excess"] <= thr
    rev2 = ev["rev_surprise"].fillna(-1) >= 0.02
    up = ev["above_200_pre"].fillna(False).astype(bool)
    return [
        ("R1 revenue beat ≥2% and dropped", rev2 & drop),
        ("R2 beat and dropped, above 200-day before", ev["any_beat"] & drop & up),
        ("R3 revenue beat ≥2%, dropped, above 200-day before", rev2 & drop & up),
    ]


def stats(ev: pd.DataFrame, h: int = PRIMARY_H) -> dict:
    """Event-level description plus a week-clustered Newey-West t."""
    col = f"excess_{h}"
    x = ev.dropna(subset=[col])
    if x.empty:
        return {"n": 0}
    weekly = x.groupby(pd.to_datetime(x["entry_date"]).dt.to_period("W-FRI"))[col].mean()
    mu, t = newey_west_t(weekly.to_numpy(), lags=max(1, math.ceil(h / 5)))
    return {
        "n": len(x),
        "weeks": len(weekly),
        "mean": float(x[col].mean()),
        "median": float(x[col].median()),
        "hit": float((x[col] > 0).mean()),
        "weekly_mean": float(mu),
        "t": t,
        "raw": float(x[f"ret_{h}"].mean()),
    }


def _fmt_row(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"| {label} | 0 | | | | | | |"
    return (
        f"| {label} | {s['n']} | {s['raw'] * 100:+.1f}% | {s['mean'] * 100:+.2f}% | {s['median'] * 100:+.2f}% "
        f"| {s['hit'] * 100:.0f}% | {s['weekly_mean'] * 100:+.2f}% | {s['t']:+.2f} |"
    )


HEAD = (
    "| Group | Events | Avg return | Avg excess vs SPY | Median excess | Beat SPY | Weekly-avg excess | NW t |\n"
    "|---|---|---|---|---|---|---|---|"
)


def report(ev: pd.DataFrame, start: date, end: date, coverage: dict, sealed: bool, threshold: float) -> str:
    thr = threshold / 100
    beat_drop = ev[ev["any_beat"] & (ev["react_excess"] <= thr)]
    L = [
        "# Earnings event study: beat and dropped",
        "",
        f"Report dates {start} → {end} ({'SEALED RUN' if sealed else 'in-sample exploration; 2021+ sealed'}). "
        f"S&P 500 point-in-time membership. Entry next open after the two-session reaction window, "
        f"{COST_BPS:.0f} bps per side. Excess is vs SPY over the same sessions. "
        "Many reports land in the same weeks, so significance is measured on weekly averages: "
        "'Weekly-avg excess' is the mean of those, and t is its Newey-West t-stat.",
        "",
        f"Events: {len(ev)} reports from {ev['ticker'].nunique()} companies. "
        f"Quality gate known for {ev['quality'].notna().sum()}; passed {int((ev['quality'] == True).sum())}. "  # noqa: E712
        f"Symbols without prices: {len(coverage.get('missing_bars', []))}. "
        f"Member-quarters with a report: {coverage.get('report_coverage', float('nan')) * 100:.0f}%.",
        "",
        f"## Beat and dropped (beat EPS or revenue, reaction ≤ {threshold:+.0f}% vs SPY), {PRIMARY_H}-session hold",
        "",
        HEAD,
        _fmt_row("Beat and dropped", stats(beat_drop)),
        _fmt_row("… quality gate passed", stats(beat_drop[beat_drop["quality"] == True])),  # noqa: E712
        _fmt_row("… quality gate failed", stats(beat_drop[beat_drop["quality"] == False])),  # noqa: E712
        _fmt_row("… beat both EPS and revenue", stats(beat_drop[beat_drop["both_beat"]])),
        _fmt_row("All beats (control)", stats(ev[ev["any_beat"]])),
        _fmt_row("All reports", stats(ev)),
        "",
        "## By reaction size, " + f"{PRIMARY_H}-session hold",
        "",
        HEAD,
    ]
    for kind, mask in (("Beat", ev["any_beat"]), ("Missed both", ~ev["any_beat"] & ev["any_miss"])):
        for lo, hi in BUCKETS:
            sub = ev[mask & (ev["react_excess"] * 100 > lo) & (ev["react_excess"] * 100 <= hi)]
            lo_s = "" if lo == -math.inf else f"{lo:+.0f}%"
            hi_s = "" if hi == math.inf else f"{hi:+.0f}%"
            rng = f"≤ {hi_s}" if not lo_s else (f"> {lo_s}" if not hi_s else f"{lo_s} to {hi_s}")
            L.append(_fmt_row(f"{kind}, reaction {rng}", stats(sub)))
    # Fixed 2026-10-10 before any refined result was seen; see REFINEMENTS.
    L += [
        "",
        f"## Pre-specified refinements, {PRIMARY_H}-session hold",
        "",
        "Defined before these rows were first computed. Pass bar (in-sample): NW t ≥ 2 and above the all-beats control. "
        "Anything that passes still has to be locked in an ADR and run once on the sealed years.",
        "",
        HEAD,
    ]
    for label, mask in refinements(ev, thr):
        s_ = stats(ev[mask])
        verdict = "pass" if s_.get("n") and s_["t"] >= 2 and s_["weekly_mean"] > stats(ev[ev["any_beat"]])["weekly_mean"] else "fail"
        L.append(_fmt_row(f"{label} — **{verdict}**", s_))
    L += ["", "## Beat and dropped across hold lengths", "", HEAD.replace("Group", "Hold (sessions)")]
    for h in HORIZONS:
        L.append(_fmt_row(str(h), stats(beat_drop, h)))
    L += ["", f"## Beat and dropped by year, {PRIMARY_H}-session hold", "", HEAD.replace("Group", "Report year")]
    for y, sub in beat_drop.groupby(pd.to_datetime(beat_drop["report_date"]).dt.year):
        L.append(_fmt_row(str(y), stats(sub)))
    L += [
        "",
        "## Read this before drawing conclusions",
        "",
        "- In-sample exploration. Slicing these tables many ways will find something that looks good by chance; "
        "only a rule locked in an ADR and then run once on the sealed years counts as evidence.",
        "- FMP's EPS actuals may mix adjusted and GAAP figures, and its estimates aren't strictly point-in-time. "
        "Borderline beats are the least reliable.",
        "- FMP's revenue estimates before ~2021 run far below the real consensus (MSFT 2016-07: $17.3B vs ~$22B; "
        "about half of 2016-2020 reports show a >5% revenue 'beat', against a median of 1-3% from 2024 on). "
        "Revenue beats and R1/R3 are unreliable in the exploration window; EPS beats look plausible.",
        "- Removed index members FMP no longer serves are missing (mild survivorship bias). Sector is today's.",
    ]
    return "\n".join(L) + "\n"


def report_coverage(ds: D.Dataset, ev: pd.DataFrame, start: date, end: date) -> float:
    """Share of (member, calendar quarter) pairs, sampled mid-quarter, that
    produced an event. Gaps are missing prices or earnings rows."""
    have = set(zip(ev["ticker"], pd.to_datetime(ev["report_date"]).dt.to_period("Q")))
    want = got = 0
    for q in pd.period_range(start, end, freq="Q"):
        mid = (q.start_time + pd.Timedelta(days=45)).date()
        for sym in ds.members_at(mid):
            want += 1
            got += (sym, q) in have
    return got / want if want else float("nan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-only", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=D.DEFAULT_CACHE)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=EXPLORE_END)
    ap.add_argument("--threshold", type=float, default=-5.0, help="max excess reaction, percent")
    ap.add_argument("--sealed-run", action="store_true", help="allow data after 2020-12-31 (once, after the ADR)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.end > EXPLORE_END and not args.sealed_run:
        ap.error(f"--end after {EXPLORE_END} reads the sealed out-of-sample years; that needs --sealed-run")

    key = "" if args.cache_only else D.api_key_from_env()
    fetcher = D.FMPFetcher(key, args.cache_dir, cache_only=args.cache_only)
    ds = D.load_dataset(fetcher, "pit500public", args.start, args.end)
    logging.info("loaded %d symbols; building events", len(ds.tickers))
    ev = build_events(ds, args.start, args.end)
    ds.coverage["report_coverage"] = report_coverage(ds, ev, args.start, args.end)
    if ev.empty:
        print("no events", file=sys.stderr)
        return 1
    md = report(ev, args.start, args.end, ds.coverage, args.sealed_run, args.threshold)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(md, encoding="utf-8")
    ev.to_csv(args.out / "events.csv", index=False)
    sys.stdout.reconfigure(encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
