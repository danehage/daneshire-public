"""
Backtest CLI. Run from backend/.

    python -m scripts.backtest.run --synthetic            # pipeline dry run, no network
    python -m scripts.backtest.run --check-plan           # which FMP endpoints our plan serves
    python -m scripts.backtest.run --fetch                # fill the FMP cache (resumable)
    python -m scripts.backtest.run                        # run on cached data, write the report

Outputs go to scripts/backtest/output/ (gitignored): report.md, picks.csv,
diagnostics.csv, decision.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

from app.services.funnel import FunnelConfig
from scripts.backtest import data as D
from scripts.backtest.engine import Engine, EngineConfig, rebalance_dates, run_backtest
from scripts.backtest.report import build_report

OUT = Path(__file__).resolve().parent / "output"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--check-plan", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--universe", choices=["pit500public", "pit500", "current900"], default="pit500public",
                    help="pit500public: public daily membership file (default; our FMP plan 402s on pit500)")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2021, 1, 1))
    ap.add_argument("--freq", choices=["W", "M"], default="W")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--random-draws", type=int, default=20)
    ap.add_argument("--no-scanner", action="store_true", help="skip the current-scanner replay (slowest variant)")
    ap.add_argument("--cache-dir", type=Path, default=D.DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--per-minute", type=int, default=280, help="FMP request budget")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.synthetic:
        ds = D.synthetic_dataset(n_tickers=80, start=date(2013, 1, 1), end=date(2022, 12, 30))
        start, end, oos = date(2015, 1, 1), date(2022, 12, 30), date(2019, 1, 1)
    else:
        fetcher = D.FMPFetcher(D.api_key_from_env(), args.cache_dir, args.per_minute)
        if args.check_plan:
            for r in D.check_plan(fetcher):
                print(f"{r['status']:<28} {r['endpoint']:<36} {r['info']}")
            return 0
        if args.fetch:
            D.fetch_all(fetcher, args.universe, args.start, args.end)
            return 0
        ds = D.load_dataset(fetcher, args.universe, args.start, args.end)
        start, end, oos = args.start, args.end, args.oos_start

    ecfg = EngineConfig(random_draws=args.random_draws, run_scanner=not args.no_scanner)
    dates = rebalance_dates(ds.spy, start, end, args.freq, ecfg.horizon)
    if not dates:
        print("no rebalance dates in range", file=sys.stderr)
        return 1
    engine = Engine(ds, FunnelConfig(), ecfg)
    t0 = time.time()

    def progress(i, n):
        if i % 10 == 0 or i == n:
            el = time.time() - t0
            logging.info("%d/%d dates  (%.0fs elapsed, ~%.0fs left)", i, n, el, el / i * (n - i))

    rows, diags = run_backtest(engine, dates, workers=args.workers, progress=progress)
    meta = {
        "run_label": f"{ds.universe_label}, {dates[0]} → {dates[-1]}",
        "synthetic": args.synthetic,
        "horizon": ecfg.horizon,
        "cost_bps": ecfg.cost_bps,
        "nw_lags": 13 if args.freq == "W" else 3,
        "oos_start": oos,
        "freq_label": "weekly (last session of each week)" if args.freq == "W" else "monthly (last session)",
    }
    md, dec = build_report(rows, diags, ds, meta)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(md, encoding="utf-8")
    rows.drop(columns=["daily"]).to_csv(args.out / "picks.csv", index=False)
    diags.to_csv(args.out / "diagnostics.csv", index=False)
    (args.out / "decision.json").write_text(json.dumps(dec, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.out / 'report.md'}  (verdict: {'ADOPT' if dec['adopt_funnel'] else 'DO NOT ADOPT'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
