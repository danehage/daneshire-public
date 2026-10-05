"""
Walk-forward replay: for each rebalance date, what every variant picked and
what those picks did over the next ``horizon`` sessions.

Conventions (fixed before any run, see docs/adr/0007):
- Signal on the rebalance date's close; entry at the NEXT session's open.
- Fixed hold of 63 sessions, exit at that close. Managed exits (stop /
  target from the funnel's trade plan) are reported alongside.
- 10 bps cost per side.
- A pick whose data ends mid-hold (acquired, delisted, ticker change) exits
  at its last close and is flagged ``truncated``.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from app.services.funnel import (
    FACTORS,
    FunnelConfig,
    TickerInput,
    bars_needed,
    compute_quality_metrics,
    prepare_cross_section,
    select_picks,
    statements_as_of,
    trade_plan,
)
from app.services.indicators import TechnicalIndicators
from app.services.scanner import StockScanner, _passes_stage1
from scripts.backtest.data import Dataset

logger = logging.getLogger(__name__)

SCANNER_TOP_N = 15


@dataclass(frozen=True)
class EngineConfig:
    horizon: int = 63
    cost_bps: float = 10.0
    random_draws: int = 20
    seed: int = 0
    sensitivity: tuple[float, ...] = (0.8, 1.2)
    run_scanner: bool = True


def rebalance_dates(spy: pd.DataFrame, start: date, end: date, freq: str, horizon: int) -> list[date]:
    """Last session of each week ('W') or month ('M') in [start, end] with a
    full ``horizon`` of SPY sessions after it."""
    dates = pd.to_datetime(spy["date"]).reset_index(drop=True)
    last_ok = len(dates) - 1 - horizon
    if last_ok < 0:
        return []
    s = pd.Series(dates[: last_ok + 1].values)
    s = s[(s >= pd.Timestamp(start)) & (s <= pd.Timestamp(end))]
    key = s.dt.to_period("W-FRI" if freq == "W" else "M")
    return [ts.date() for ts in s.groupby(key).max()]


class _Arrays:
    __slots__ = ("dates", "open", "high", "low", "close", "volume", "frame")

    def __init__(self, df: pd.DataFrame):
        self.frame = df.reset_index(drop=True)
        self.dates = pd.to_datetime(self.frame["date"]).values.astype("datetime64[D]")
        self.open = self.frame["open"].to_numpy(float)
        self.high = self.frame["high"].to_numpy(float)
        self.low = self.frame["low"].to_numpy(float)
        self.close = self.frame["close"].to_numpy(float)
        self.volume = self.frame["volume"].to_numpy(float)

    def idx_after(self, d: date) -> int:
        return int(np.searchsorted(self.dates, np.datetime64(d, "D"), side="right"))


class Engine:
    def __init__(self, ds: Dataset, cfg: FunnelConfig = FunnelConfig(), ecfg: EngineConfig = EngineConfig()):
        self.ds, self.cfg, self.ecfg = ds, cfg, ecfg
        self.arr = {s: _Arrays(t["bars"]) for s, t in ds.tickers.items()}
        self.spy = _Arrays(ds.spy)
        self.slice_len = max(bars_needed(cfg.scaled(k)) for k in (1.0, *ecfg.sensitivity)) + 5
        self.scanner = StockScanner.__new__(StockScanner)  # no client / cache dirs
        self.scanner.indicators = TechnicalIndicators()

    # -- inputs ------------------------------------------------------------

    def inputs_at(self, d: date) -> list[TickerInput]:
        out = []
        for sym in sorted(self.ds.members_at(d)):
            t = self.ds.tickers.get(sym)
            if t is None:
                continue
            a = self.arr[sym]
            end = a.idx_after(d)
            if end == 0:
                continue
            bars = a.frame.iloc[max(0, end - self.slice_len) : end]
            out.append(TickerInput(sym, t["sector"], bars, t["income"], t["balance"], t["cashflow"], t["earnings"]))
        return out

    # -- outcomes ------------------------------------------------------------

    def outcome(self, sym: Optional[str], d: date, stop_ratio: float, target_ratio: float) -> Optional[dict]:
        h, cost = self.ecfg.horizon, self.ecfg.cost_bps / 1e4
        a = self.spy if sym is None else self.arr[sym]
        i = a.idx_after(d)
        if i >= len(a.dates):
            return None
        last = min(i + h - 1, len(a.dates) - 1)
        truncated = (i + h - 1) > (len(a.dates) - 1)
        entry = a.open[i]
        if not entry or entry <= 0:
            return None
        closes = a.close[i : last + 1]
        ret = closes[-1] / entry - 1 - 2 * cost

        daily = np.empty(h)
        daily[:] = 0.0  # cash after a truncated exit
        seg = np.concatenate([[entry], closes])
        daily[: len(closes)] = seg[1:] / seg[:-1] - 1
        daily[0] -= cost
        daily[len(closes) - 1] -= cost

        lows, highs, opens = a.low[i : last + 1], a.high[i : last + 1], a.open[i : last + 1]
        mae = float(lows.min() / entry - 1)
        stop_px, tgt_px, ten = entry * stop_ratio, entry * target_ratio, entry * 1.10
        managed, touched_first, stopped = None, False, False
        for k in range(len(closes)):
            o, hi, lo = opens[k], highs[k], lows[k]
            if not stopped and not touched_first and hi >= ten and lo > stop_px:
                touched_first = True
            if managed is None:
                if o <= stop_px:
                    managed, stopped = o / entry - 1, True
                elif lo <= stop_px:
                    managed, stopped = stop_px / entry - 1, True
                elif o >= tgt_px:
                    managed = o / entry - 1
                elif hi >= tgt_px:
                    managed = tgt_px / entry - 1
            if stopped and not touched_first:
                break
        if managed is None:
            managed = closes[-1] / entry - 1
        return {
            "ret": float(ret),
            "managed_ret": float(managed - 2 * cost),
            "hit10": bool(ret >= 0.10),
            "touched10_first": bool(touched_first),
            "mae": mae,
            "truncated": bool(truncated),
            "daily": daily,
        }

    # -- one date ---------------------------------------------------------------

    def run_date(self, d: date) -> tuple[list[dict], dict]:
        cfg, rows = self.cfg, []
        inputs = self.inputs_at(d)
        spy_out = self.outcome(None, d, 0.0, 1e9)
        if spy_out is None:
            return [], {}
        spy_ret = spy_out["ret"]
        cs = prepare_cross_section(inputs, d, self.ds.spy, cfg)

        def plan(sym):
            tech = cs.techs.get(sym)
            if tech is None:
                return 0.90, 1.15
            _, stop, target = trade_plan(tech, cfg)
            return stop / tech["price"], target / tech["price"]

        def add(variant, sym, lane=None, tier=None, plan_ratios=None, draw=None):
            sr, tr = plan_ratios or plan(sym)
            o = self.outcome(sym, d, sr, tr)
            if o is None:
                return
            rows.append({
                "variant": variant, "date": d, "ticker": sym, "lane": lane, "tier": tier, "draw": draw,
                "spy_ret": spy_ret, "excess": o["ret"] - spy_ret, **o,
            })

        variants = {
            "funnel": select_picks(cs, cfg),
            "funnel_no_quality_gate": select_picks(cs, cfg, use_quality_gate=False),
            "funnel_no_risk_screen": select_picks(cs, cfg, use_risk_screen=False),
            "funnel_momentum_only": select_picks(cs, cfg, use_recovery=False),
        }
        for f in FACTORS:
            variants[f"funnel_without_{f}"] = select_picks(cs, cfg, factors=tuple(x for x in FACTORS if x != f))
        for k in self.ecfg.sensitivity:
            ck = cfg.scaled(k)
            variants[f"funnel_lookback_x{k}"] = select_picks(prepare_cross_section(inputs, d, self.ds.spy, ck), ck)
        for name, res in variants.items():
            for p in res.picks:
                add(name, p.ticker, p.lane, p.tier, (p.stop / p.price, p.target / p.price))

        funnel = variants["funnel"]
        n_funnel = len(funnel.picks)
        rng = np.random.default_rng(self.ecfg.seed * 1_000_003 + d.toordinal())
        pool = sorted(cs.quality_ok)
        if n_funnel and pool:
            for k in range(self.ecfg.random_draws):
                for sym in rng.choice(pool, size=min(n_funnel, len(pool)), replace=False):
                    add("random_quality_gate", str(sym), draw=k)

        for sym in cs.techs:
            add("universe_equal_weight", sym)

        if self.ecfg.run_scanner:
            for sym in self.scanner_top(inputs, d):
                add("scanner_top15", sym)

        rows.append({"variant": "spy", "date": d, "ticker": "SPY", "lane": None, "tier": None, "draw": None,
                     "spy_ret": spy_ret, "excess": 0.0, **spy_out})
        diag = {
            "date": d,
            "risk_on": cs.risk_on,
            "breadth": cs.breadth,
            **{f"n_{k}": v for k, v in funnel.stage_counts.items()},
        }
        return rows, diag

    def scanner_top(self, inputs: list[TickerInput], d: date) -> list[str]:
        """Replay the CURRENT live scanner (Stage 1 + _score_opportunity)
        on bars as of ``d``. Stage 1 sees the unadjusted price/volume and the
        historical market cap (else adjusted close x last-filed diluted
        shares, which FMP split-adjusts), as a live quote would have; the analysis sees the adjusted
        price so it stays consistent with the adjusted bars."""
        scored = []
        for ti in inputs:
            df = ti.bars.tail(252).reset_index(drop=True)
            if len(df) < 20:
                continue
            last = df.iloc[-1]
            price = float(last["close"])
            raw_price = _positive(last.get("raw_close")) or price
            raw_vol = _positive(last.get("raw_volume")) or float(last["volume"])
            mcap = _positive(last.get("market_cap"))
            if mcap is None:
                mcap = 0.0
                inc = statements_as_of(ti.income, d, self.cfg.filing_lag_fallback_days)
                if inc:
                    shares = inc[0].get("weightedAverageShsOutDil") or inc[0].get("weightedAverageShsOut")
                    mcap = price * float(shares) if shares else 0.0
            if not _passes_stage1({"price": raw_price, "volume": raw_vol, "marketCap": mcap}):
                continue
            quote = {"price": price, "volume": raw_vol, "marketCap": mcap}
            try:
                res = self.scanner._analyze_frame(ti.ticker, quote, df)
            except Exception:  # noqa: BLE001 — mirror the live scanner's skip
                res = None
            if res is None:
                continue
            score, _, _ = StockScanner._score_opportunity(res)
            scored.append((score, ti.ticker))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [t for _, t in scored[:SCANNER_TOP_N]]


# ---------------------------------------------------------------------------
# Parallel driver
# ---------------------------------------------------------------------------

def _positive(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) and f > 0 else None


_ENGINE: Optional[Engine] = None


def _init_worker(engine: Engine) -> None:
    global _ENGINE
    _ENGINE = engine


def _run_one(d: date):
    try:
        return _ENGINE.run_date(d)
    except Exception as e:  # noqa: BLE001
        logger.exception("date %s failed", d)
        return [], {"date": d, "error": str(e)}


def run_backtest(engine: Engine, dates: list[date], workers: int = 1, progress=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    global _ENGINE
    _ENGINE = engine
    all_rows, diags = [], []
    if workers > 1:
        # fork shares the loaded dataset for free; Windows has no fork, so
        # spawn pickles the engine to each worker once, via the initializer.
        method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
        ctx = mp.get_context(method)
        with ctx.Pool(workers, initializer=_init_worker, initargs=(engine,)) as pool:
            for i, (rows, diag) in enumerate(pool.imap(_run_one, dates, chunksize=1)):
                all_rows.extend(rows)
                diags.append(diag)
                if progress:
                    progress(i + 1, len(dates))
    else:
        for i, d in enumerate(dates):
            rows, diag = _run_one(d)
            all_rows.extend(rows)
            diags.append(diag)
            if progress:
                progress(i + 1, len(dates))
    return pd.DataFrame(all_rows), pd.DataFrame(diags)
