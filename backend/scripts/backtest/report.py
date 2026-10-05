"""Markdown report for a backtest run."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from scripts.backtest import metrics as M

COMPARATORS = ["funnel", "scanner_top15", "random_quality_gate", "universe_equal_weight", "spy"]
NAMED_WINDOWS = [
    ("2018 Q4 selloff", date(2018, 10, 1), date(2018, 12, 31)),
    ("2020 COVID crash", date(2020, 2, 15), date(2020, 4, 30)),
    ("2022 bear market", date(2022, 1, 1), date(2022, 10, 31)),
    ("2025 Feb-Apr drawdown", date(2025, 2, 15), date(2025, 4, 30)),
]


def _p(x, pct=True, digits=1):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    return f"{x * 100:+.{digits}f}%" if pct else f"{x:.{digits}f}"


def _table(summaries: list[dict]) -> str:
    head = ("| Variant | Picks | /date | Empty | Hit +10% | +10% before stop | Mean | Median | "
            "Avg win | Avg loss | MAE | Managed | Cohort excess vs SPY | NW t | Max DD | CAGR | Turnover |")
    sep = "|" + "---|" * 17
    lines = [head, sep]
    for s in summaries:
        lines.append(
            f"| {s['variant']} | {s['picks']} | {s['picks_per_date']:.1f} | {s['empty_dates']} | "
            f"{_p(s['hit10']).lstrip('+')} | {_p(s['touched10_first']).lstrip('+')} | {_p(s['mean_ret'])} | "
            f"{_p(s['median_ret'])} | {_p(s['avg_win'])} | {_p(s['avg_loss'])} | {_p(s['mean_mae'])} | "
            f"{_p(s['managed_mean_ret'])} | {_p(s['cohort_excess'])} | {_p(s['cohort_excess_t'], pct=False, digits=2)} | "
            f"{_p(s['equity_max_dd'])} | {_p(s['equity_cagr'])} | {_p(s['turnover']).lstrip('+')} |"
        )
    return "\n".join(lines)


def build_report(rows: pd.DataFrame, diags: pd.DataFrame, ds, meta: dict) -> tuple[str, dict]:
    h, lags = meta["horizon"], meta["nw_lags"]
    spy_dates = pd.DatetimeIndex(pd.to_datetime(ds.spy["date"]))
    all_dates = sorted(diags["date"].dropna().unique()) if len(diags) else []
    split = meta["oos_start"]
    is_dates = [d for d in all_dates if d < split]
    oos_dates = [d for d in all_dates if d >= split]
    variants = [v for v in COMPARATORS if v in set(rows["variant"])]
    ablations = sorted(v for v in set(rows["variant"]) if v.startswith("funnel_"))

    def block(dates, names):
        return [M.summarize(rows, v, dates, spy_dates, h, lags) for v in names] if dates else []

    oos = {s["variant"]: s for s in block(oos_dates, variants + ablations)}
    dec = M.decision(rows, oos, oos_dates, lags) if oos_dates else {"adopt_funnel": False, "comparisons": {}, "recovery_lane": {}}

    L = []
    L.append(f"# Funnel backtest — {meta['run_label']}\n")
    if meta.get("synthetic"):
        L.append("> **SYNTHETIC DATA.** This run exercises the pipeline on a random-walk market. "
                 "Nothing below is evidence about real stocks.\n")
    if ds.survivorship_biased:
        L.append("> **SURVIVORSHIP-BIASED UNIVERSE.** Today's index members replayed into the past: "
                 "names that blew up and were removed are missing. Treat every number as optimistic.\n")
    verdict = "ADOPT" if dec["adopt_funnel"] else "DO NOT ADOPT"
    L.append(f"## Verdict (pre-registered rule, out-of-sample {split} onward): **{verdict}**\n")
    for comp, c in dec["comparisons"].items():
        if not c.get("available", True):
            L.append(f"- vs `{comp}`: comparator not run — rule cannot pass.")
            continue
        L.append(f"- vs `{comp}`: mean cohort difference {_p(c['mean_diff'], digits=2)}, NW t {_p(c['t'], pct=False, digits=2)}, "
                 f"drawdown no worse: {'yes' if c['dd_ok'] else 'no'} → **{'pass' if c['passed'] else 'fail'}**")
    r = dec.get("recovery_lane", {})
    if r:
        L.append(f"- Recovery lane vs random quality-gate picks: {r.get('picks', 0)} picks, mean diff "
                 f"{_p(r.get('mean_diff'), digits=2)}, t {_p(r.get('t'), pct=False, digits=2)} → "
                 f"**{'keep' if r.get('keep') else 'kill'}**")
    L.append("")
    L.append("Rule: adopt only if the funnel's per-date cohort return beats BOTH the current scanner and random "
             "picks from the same quality-gated pool with Newey-West t > 2, without a worse equity max drawdown. "
             "Thresholds were fixed before this run; nothing was tuned on it.\n")

    L.append("## Setup\n")
    cov = ds.coverage
    L.append(f"- Universe: {ds.universe_label}; {cov.get('symbols_ever_member')} symbols ever members, "
             f"{cov.get('with_bars')} with price data ({len(cov.get('missing_bars', []))} missing: residual survivorship bias), "
             f"{len(cov.get('missing_fundamentals', []))} without statements (fail the quality gate).")
    if "missing_unadjusted_prices" in cov:
        L.append(f"- Dollar thresholds use unadjusted prices and historical market cap. Fell back to adjusted "
                 f"prices for {len(cov['missing_unadjusted_prices'])} symbols and to adjusted price x split-adjusted shares for "
                 f"{len(cov['missing_market_cap'])}; those can be wrongly screened out ahead of a later split.")
    L.append(f"- Rebalance: {meta['freq_label']}, {len(all_dates)} dates "
             f"({all_dates[0] if all_dates else '–'} → {all_dates[-1] if all_dates else '–'}); "
             f"in-sample {len(is_dates)}, out-of-sample {len(oos_dates)}.")
    L.append(f"- Signal at the rebalance close, entry next open, {h}-session hold, {meta['cost_bps']:.0f} bps per side. "
             f"Managed = funnel stop/target applied on daily bars (stop assumed first on a both-hit day).")
    L.append(f"- Statistics: cohort = one rebalance date; empty cohorts hold cash. Newey-West lags = {lags}.")
    L.append(f"- Truncated holds (data ended mid-hold, exited at last close): "
             f"{int(rows.loc[rows['variant'] == 'funnel', 'truncated'].sum()) if len(rows) else 0} funnel picks.\n")

    for label, dates in (("Out-of-sample", oos_dates), ("In-sample", is_dates), ("Full period", all_dates)):
        if not dates:
            continue
        L.append(f"## {label} — main comparison\n")
        L.append(_table(block(dates, variants)))
        L.append("")

    if oos_dates and ablations:
        L.append("## Out-of-sample — ablations and sensitivity\n")
        L.append("Each row removes one piece of the funnel or scales every lookback. A piece that doesn't hurt "
                 "when removed isn't earning its place; results that flip under ±20% lookbacks are fragile.\n")
        L.append(_table([oos["funnel"]] + [oos[a] for a in ablations if a in oos]))
        L.append("")

    if len(diags) and "risk_on" in diags:
        on = [d for d in all_dates if bool(diags.set_index("date").loc[d, "risk_on"])]
        off = [d for d in all_dates if d not in set(on)]
        L.append("## By regime\n")
        for label, dates in (("Risk-on dates", on), ("Risk-off dates", off)):
            if dates:
                L.append(f"### {label} ({len(dates)})\n")
                L.append(_table(block(dates, variants)))
                L.append("")
        for label, a, b in NAMED_WINDOWS:
            dates = [d for d in all_dates if a <= d <= b]
            if dates:
                L.append(f"### {label} ({len(dates)} signal dates)\n")
                L.append(_table(block(dates, variants)))
                L.append("")

    if len(diags):
        L.append("## Funnel stage counts (median per date)\n")
        cols = [c for c in diags.columns if c.startswith("n_")]
        L.append("| " + " | ".join(c[2:] for c in cols) + " |")
        L.append("|" + "---|" * len(cols))
        L.append("| " + " | ".join(f"{diags[c].median():.0f}" for c in cols) + " |\n")

    L.append("## Caveats\n")
    L.append("- Next-earnings dates for the blackout use the actual report date; companies usually confirm 2-4 weeks "
             "ahead, so inside a 14-day window this is close to what was knowable.")
    L.append("- Sector is today's FMP profile sector for every date.")
    L.append("- Removed index members whose tickers FMP no longer serves are missing (count above).")
    L.append("- Estimate revisions are not tested: FMP has no point-in-time revision history.")
    return "\n".join(L) + "\n", dec
