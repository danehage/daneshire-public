"""
Conviction funnel: ~900 names -> 10-20 picks (see docs/adr/0007).

Four stages, each a pure function of point-in-time inputs:

1. Quality gate — hard fundamental exclusions (dumpster filter).
2. Setup score — equal-weight percentile ranks of three factors:
   A relative strength (6m skip 2w, vs universe and vs sector),
   B proximity to the 52-week high (behind a trend-template gate),
   C last-report earnings momentum (EPS + revenue surprise, post-report drift).
   D (volatility contraction + accumulation) is a tie-breaker only.
   A capped recovery lane takes quality names >= 20% off highs that beat
   their last report and reclaimed the 50-DMA.
3. Risk screen — earnings blackout, ATR cap, sector cap, pairwise
   correlation cap, market regime (sets list size).
4. Tiering + trade plan (entry / stop / target) + one-line thesis.

Every threshold lives in ``FunnelConfig`` and was fixed BEFORE any backtest
ran. Do not tune them against backtest output — that is the overfitting the
funnel exists to avoid. ``run_funnel`` truncates every input to ``as_of``
itself, so callers may pass full histories (the backtest does) without
leaking the future.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from app.services.indicators import TechnicalIndicators

# FMP profile sector names that need sector-specific fundamentals rules.
FINANCIAL_SECTORS = frozenset({"Financial Services", "Real Estate"})
NO_LEVERAGE_TEST_SECTORS = frozenset({"Financial Services", "Real Estate", "Utilities"})
NO_ALTMAN_SECTORS = frozenset({"Financial Services", "Real Estate", "Utilities"})

FACTORS = ("A", "B", "C")


@dataclass(frozen=True)
class FunnelConfig:
    # Stage 1 — quality gate
    min_market_cap: float = 2e9
    min_price: float = 10.0
    min_dollar_volume: float = 20e6
    max_net_debt_ebitda: float = 3.5
    max_dilution_pct: float = 5.0
    min_altman_z: float = 1.8
    # Statements with no filing date are assumed public this long after
    # period end (10-Q deadline is 40-45 days).
    filing_lag_fallback_days: int = 45

    # Stage 2 — setup factors (bar counts)
    rs_lookback: int = 126
    rs_skip: int = 10
    ma_fast: int = 50
    ma_slow: int = 200
    ma_slow_slope_lookback: int = 20
    high_lookback: int = 252
    hv_short: int = 20
    hv_long: int = 120
    accumulation_window: int = 50
    earnings_recency_days: int = 90

    # Recovery lane
    recovery_drawdown_pct: float = 20.0
    recovery_slots: int = 5

    # Stage 3 — risk screen
    earnings_blackout_days: int = 14  # calendar days ~= 10 sessions
    max_atr_pct: float = 5.0
    max_per_sector: int = 3
    max_pair_corr: float = 0.7
    corr_window: int = 60
    breadth_risk_on: float = 0.5
    n_risk_on: int = 15
    n_risk_off: int = 6

    # Stage 4 — tiers + trade plan
    tier_a: float = 0.90
    tier_b: float = 0.75
    stop_atr_mult: float = 2.0
    max_risk_pct: float = 10.0
    target_min_pct: float = 15.0
    target_r_multiple: float = 2.5
    hold_days: int = 63

    def scaled(self, k: float) -> "FunnelConfig":
        """Copy with every lookback scaled by ``k`` (sensitivity runs only)."""
        s = lambda n: max(2, int(round(n * k)))  # noqa: E731
        return replace(
            self,
            rs_lookback=s(self.rs_lookback),
            rs_skip=s(self.rs_skip),
            ma_fast=s(self.ma_fast),
            ma_slow=s(self.ma_slow),
            ma_slow_slope_lookback=s(self.ma_slow_slope_lookback),
            hv_short=s(self.hv_short),
            hv_long=s(self.hv_long),
            accumulation_window=s(self.accumulation_window),
            corr_window=s(self.corr_window),
        )


# --------------------------------------------------------------------------
# Inputs / outputs
# --------------------------------------------------------------------------


@dataclass
class TickerInput:
    """Everything the funnel needs for one ticker. Histories may extend past
    ``as_of``; ``run_funnel`` truncates them."""

    ticker: str
    sector: Optional[str]
    # date, open, high, low, close, volume — oldest first. Optional columns
    # raw_close / raw_volume (unadjusted) and market_cap (point-in-time) feed
    # the dollar thresholds; see compute_technicals.
    bars: pd.DataFrame
    income: list[dict] = field(default_factory=list)  # FMP quarterly
    balance: list[dict] = field(default_factory=list)
    cashflow: list[dict] = field(default_factory=list)
    earnings: list[dict] = field(default_factory=list)  # FMP /stable/earnings


@dataclass
class QualityMetrics:
    market_cap: Optional[float]
    fcf_ttm: Optional[float]
    net_income_ttm: Optional[float]
    revenue_yoy_pct: Optional[float]
    revenue_down_2q: Optional[bool]
    net_debt_ebitda: Optional[float]
    dilution_pct: Optional[float]
    altman_z: Optional[float]
    latest_filing: Optional[date]


@dataclass
class Pick:
    ticker: str
    sector: Optional[str]
    lane: str  # "momentum" | "recovery"
    tier: str  # "A" | "B"
    composite: float
    factor_pcts: dict
    thesis: str
    price: float
    entry_note: str
    stop: float
    target: float
    next_earnings: Optional[date] = None


@dataclass
class FunnelResult:
    as_of: date
    risk_on: bool
    breadth: Optional[float]
    stage_counts: dict
    picks: list[Pick]
    # Diagnostics for the backtest: everyone who passed the quality gate.
    quality_passers: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Point-in-time helpers
# --------------------------------------------------------------------------


def _to_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):  # incl. pd.Timestamp
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return pd.Timestamp(v).date()
    except (ValueError, TypeError):
        return None


def statement_available_date(stmt: dict, fallback_days: int = 45) -> Optional[date]:
    """First date a statement was public: filing/accepted date, else period
    end + ``fallback_days``. Using the period-end date would be look-ahead."""
    for key in ("filingDate", "acceptedDate", "fillingDate"):
        d = _to_date(stmt.get(key))
        if d:
            return d
    period = _to_date(stmt.get("date"))
    return period + timedelta(days=fallback_days) if period else None


def statements_as_of(
    stmts: Iterable[dict], as_of: date, fallback_days: int = 45
) -> list[dict]:
    """Statements public strictly before ``as_of``, newest period first."""
    out = []
    for s in stmts or []:
        avail = statement_available_date(s, fallback_days)
        if avail is not None and avail < as_of:
            out.append(s)
    out.sort(key=lambda s: _to_date(s.get("date")) or date.min, reverse=True)
    return out


def bars_as_of(bars: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Bars dated on or before ``as_of`` (the signal uses that day's close)."""
    if bars is None or bars.empty:
        return bars
    dates = pd.to_datetime(bars["date"])
    cut = int(dates.searchsorted(pd.Timestamp(as_of), side="right"))
    return bars.iloc[:cut]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sum(stmts: list[dict], key: str) -> Optional[float]:
    vals = [_num(s.get(key)) for s in stmts]
    if not vals or any(v is None for v in vals):
        return None
    return float(sum(vals))


# --------------------------------------------------------------------------
# Stage 1 — quality gate
# --------------------------------------------------------------------------


def compute_quality_metrics(
    income: list[dict],
    balance: list[dict],
    cashflow: list[dict],
    as_of: date,
    price: float,
    cfg: FunnelConfig = FunnelConfig(),
    market_cap: Optional[float] = None,
) -> Optional[QualityMetrics]:
    """TTM fundamentals as known on ``as_of``. None without 4 quarters.

    ``price`` is only used for the price x shares market cap, so it must be
    on the shares' basis: FMP split-adjusts historical share counts, so pass
    the adjusted price (live, adjusted equals as-traded). ``market_cap``, when
    known (the backtest's historical series), overrides price x shares."""
    lag = cfg.filing_lag_fallback_days
    inc = statements_as_of(income, as_of, lag)
    bal = statements_as_of(balance, as_of, lag)
    cf = statements_as_of(cashflow, as_of, lag)
    if len(inc) < 4:
        return None

    q4 = inc[:4]
    shares = _num(q4[0].get("weightedAverageShsOutDil")) or _num(
        q4[0].get("weightedAverageShsOut")
    )
    if market_cap is None and shares and price:
        market_cap = price * shares

    revenue_ttm = _sum(q4, "revenue")
    net_income_ttm = _sum(q4, "netIncome")
    revenue_yoy = None
    revenue_down_2q = None
    if len(inc) >= 8:
        prior = _sum(inc[4:8], "revenue")
        if revenue_ttm is not None and prior and prior > 0:
            revenue_yoy = (revenue_ttm / prior - 1) * 100
        r = [_num(s.get("revenue")) for s in inc[:6]]
        if None not in r:
            revenue_down_2q = r[0] < r[4] and r[1] < r[5]

    dilution = None
    if len(inc) >= 5:
        s_now = _num(inc[0].get("weightedAverageShsOutDil"))
        s_ago = _num(inc[4].get("weightedAverageShsOutDil"))
        if s_now and s_ago and s_ago > 0:
            dilution = (s_now / s_ago - 1) * 100

    fcf_ttm = None
    if len(cf) >= 4:
        fcf_ttm = _sum(cf[:4], "freeCashFlow")
        if fcf_ttm is None:
            ocf = _sum(cf[:4], "operatingCashFlow")
            capex = _sum(cf[:4], "capitalExpenditure")
            if ocf is not None and capex is not None:
                fcf_ttm = ocf - abs(capex)

    ebitda_ttm = _sum(q4, "ebitda")
    if ebitda_ttm is None and len(cf) >= 4:
        op = _sum(q4, "operatingIncome")
        da = _sum(cf[:4], "depreciationAndAmortization")
        if op is not None and da is not None:
            ebitda_ttm = op + da

    net_debt_ebitda = None
    altman = None
    if bal:
        b = bal[0]
        net_debt = _num(b.get("netDebt"))
        if net_debt is None:
            debt, cash = _num(b.get("totalDebt")), _num(b.get("cashAndCashEquivalents"))
            if debt is not None and cash is not None:
                net_debt = debt - cash
        if net_debt is not None and ebitda_ttm is not None:
            if net_debt <= 0:
                net_debt_ebitda = 0.0
            elif ebitda_ttm > 0:
                net_debt_ebitda = net_debt / ebitda_ttm
            else:
                net_debt_ebitda = math.inf

        ta = _num(b.get("totalAssets"))
        tl = _num(b.get("totalLiabilities"))
        ca = _num(b.get("totalCurrentAssets"))
        cl = _num(b.get("totalCurrentLiabilities"))
        re_ = _num(b.get("retainedEarnings"))
        ebit = _sum(q4, "operatingIncome")
        if None not in (ta, tl, ca, cl, re_, ebit, revenue_ttm, market_cap) and ta > 0 and tl > 0:
            altman = (
                1.2 * (ca - cl) / ta
                + 1.4 * re_ / ta
                + 3.3 * ebit / ta
                + 0.6 * market_cap / tl
                + 1.0 * revenue_ttm / ta
            )

    return QualityMetrics(
        market_cap=market_cap,
        fcf_ttm=fcf_ttm,
        net_income_ttm=net_income_ttm,
        revenue_yoy_pct=revenue_yoy,
        revenue_down_2q=revenue_down_2q,
        net_debt_ebitda=net_debt_ebitda,
        dilution_pct=dilution,
        altman_z=altman,
        latest_filing=statement_available_date(inc[0], lag),
    )


def passes_quality_gate(
    m: Optional[QualityMetrics],
    sector: Optional[str],
    price: float,
    dollar_volume: Optional[float],
    cfg: FunnelConfig = FunnelConfig(),
) -> tuple[bool, list[str]]:
    """(passed, failure reasons). Missing data fails — an unverifiable
    company is not a quality company."""
    fails: list[str] = []
    if price < cfg.min_price:
        fails.append("price")
    if dollar_volume is None or dollar_volume < cfg.min_dollar_volume:
        fails.append("liquidity")
    if m is None:
        return False, fails + ["no fundamentals"]
    if m.market_cap is None or m.market_cap < cfg.min_market_cap:
        fails.append("market cap")

    if sector in FINANCIAL_SECTORS:
        if m.net_income_ttm is None or m.net_income_ttm <= 0:
            fails.append("net income")
    elif m.fcf_ttm is None or m.fcf_ttm <= 0:
        fails.append("free cash flow")

    if m.revenue_yoy_pct is None or m.revenue_yoy_pct < 0:
        fails.append("revenue shrinking")
    if m.revenue_down_2q:
        fails.append("revenue down 2Q")
    if m.dilution_pct is not None and m.dilution_pct > cfg.max_dilution_pct:
        fails.append("dilution")
    if sector not in NO_LEVERAGE_TEST_SECTORS:
        if m.net_debt_ebitda is None or m.net_debt_ebitda > cfg.max_net_debt_ebitda:
            fails.append("leverage")
    if sector not in NO_ALTMAN_SECTORS:
        if m.altman_z is None or m.altman_z < cfg.min_altman_z:
            fails.append("altman z")
    return not fails, fails


# --------------------------------------------------------------------------
# Stage 2 — factor inputs
# --------------------------------------------------------------------------


def _confirmed_swing_low(lows: pd.Series, window: int = 5, lookback: int = 60) -> Optional[float]:
    """Newest swing low with ``window`` bars on BOTH sides (never repaints)."""
    n = len(lows)
    for i in range(n - 1 - window, max(window, n - lookback) - 1, -1):
        seg = lows.iloc[i - window : i + window + 1]
        if lows.iloc[i] == seg.min():
            return float(lows.iloc[i])
    return None


def _col(df: pd.DataFrame, name: str, fallback: pd.Series) -> pd.Series:
    """Optional numeric column, gaps filled from ``fallback``."""
    if name not in df.columns:
        return fallback
    s = pd.to_numeric(df[name], errors="coerce").astype(float).reset_index(drop=True)
    return s.where(s > 0, fallback)


def compute_technicals(df: pd.DataFrame, cfg: FunnelConfig = FunnelConfig()) -> Optional[dict]:
    """Bar-derived factor inputs from bars already truncated to as-of."""
    need = max(cfg.ma_slow + cfg.ma_slow_slope_lookback, cfg.rs_lookback + 1, cfg.hv_long + 1)
    if df is None or len(df) < need:
        return None
    close = df["close"].astype(float).reset_index(drop=True)
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    vol = df["volume"].astype(float).reset_index(drop=True)
    px = float(close.iloc[-1])
    if px <= 0:
        return None

    ret_6m = float(close.iloc[-1 - cfg.rs_skip] / close.iloc[-1 - cfg.rs_lookback] - 1)
    ma_fast = float(close.tail(cfg.ma_fast).mean())
    ma_slow_series = close.rolling(cfg.ma_slow).mean()
    ma_slow = float(ma_slow_series.iloc[-1])
    ma_slow_prev = float(ma_slow_series.iloc[-1 - cfg.ma_slow_slope_lookback])
    trend_ok = px > ma_fast > ma_slow and ma_slow > ma_slow_prev

    high_n = float(high.tail(cfg.high_lookback).max())
    prox = px / high_n if high_n > 0 else 0.0

    rets = close.pct_change()
    hv_s = float(rets.tail(cfg.hv_short).std())
    hv_l = float(rets.tail(cfg.hv_long).std())
    hv_ratio = hv_s / hv_l if hv_l > 0 else 1.0

    w = cfg.accumulation_window
    d = close.diff().tail(w)
    v = vol.tail(w)
    up_v, dn_v = float(v[d > 0].sum()), float(v[d < 0].sum())
    updown = up_v / dn_v if dn_v > 0 else 2.0

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1
    ).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1])

    # Dollar thresholds (price floor, liquidity, market cap) need what the
    # tape showed that day. Adjusted history understates the past price of
    # anything that later split or paid dividends — NVDA's 2016 close is
    # ~$1 after its splits — which would screen out future winners using
    # knowledge of their future splits. Live bars end today, where adjusted
    # equals unadjusted, so these fall back to close/volume.
    raw_close = _col(df, "raw_close", close)
    raw_vol = _col(df, "raw_volume", vol)
    mcap = None
    if "market_cap" in df.columns:
        known = pd.to_numeric(df["market_cap"], errors="coerce").dropna()
        if len(known) and known.iloc[-1] > 0:
            mcap = float(known.iloc[-1])

    return {
        "price": px,
        "raw_price": float(raw_close.iloc[-1]),
        "market_cap": mcap,
        "ret_6m": ret_6m,
        "trend_ok": bool(trend_ok),
        "above_ma_fast": px > ma_fast,
        "above_ma_slow": px > ma_slow,
        "ma_fast": ma_fast,
        "ma_slow": ma_slow,
        "prox_high": prox,
        "high_52w": high_n,
        "drawdown_pct": (prox - 1) * 100,
        "hv_ratio": hv_ratio,
        "updown_vol": updown,
        "atr": atr,
        "atr_pct": atr / px * 100,
        "dollar_volume": float((raw_close * raw_vol).tail(20).mean()),
        "high_10d": float(high.tail(10).max()),
        "ema_21": float(close.ewm(span=21, adjust=False).mean().iloc[-1]),
        "swing_low": _confirmed_swing_low(low),
        "returns": rets.tail(cfg.corr_window).to_numpy(),
    }


def compute_earnings_factors(
    earnings: list[dict], df: pd.DataFrame, as_of: date, cfg: FunnelConfig = FunnelConfig()
) -> dict:
    """Last report's surprise + post-report drift, plus next report date.

    Only reports dated strictly before ``as_of`` count: a report after the
    close on ``as_of`` is not in that day's signal.
    """
    past, future = [], []
    for e in earnings or []:
        d = _to_date(e.get("date"))
        if d is None:
            continue
        if d < as_of and _num(e.get("epsActual")) is not None:
            past.append((d, e))
        elif d >= as_of:
            future.append(d)
    out: dict = {
        "next_earnings": min(future) if future else None,
        "eps_surprise": None,
        "rev_surprise": None,
        "drift": None,
        "report_date": None,
    }
    if not past:
        return out
    d, e = max(past, key=lambda p: p[0])
    if (as_of - d).days > cfg.earnings_recency_days:
        return out
    out["report_date"] = d
    act, est = _num(e.get("epsActual")), _num(e.get("epsEstimated"))
    if act is not None and est is not None:
        out["eps_surprise"] = float(np.clip((act - est) / max(abs(est), 0.01), -2, 2))
    ra, re_ = _num(e.get("revenueActual")), _num(e.get("revenueEstimated"))
    if ra is not None and re_ and re_ > 0:
        out["rev_surprise"] = float(np.clip(ra / re_ - 1, -1, 1))
    if df is not None and len(df):
        dates = pd.to_datetime(df["date"])
        i = int(dates.searchsorted(pd.Timestamp(d), side="left")) - 1
        if i >= 0:
            pre = float(df["close"].iloc[i])
            if pre > 0:
                out["drift"] = float(df["close"].iloc[-1]) / pre - 1
    return out


def recovery_points(df: pd.DataFrame, tech: dict) -> Optional[int]:
    """The existing scanner's recovery-track points (no market-cap bonus),
    reused as the recovery lane's tie-breaker."""
    from app.services.scanner import StockScanner

    rec = TechnicalIndicators.calculate_recovery_metrics(tech["price"], df.reset_index(drop=True))
    if rec is None:
        return None
    result = {
        "price": tech["price"],
        "high_52w": tech["high_52w"],
        "trend": "Unknown",
        "market_cap": 0,
        **rec,
    }
    score, _, _ = StockScanner._score_opportunity(result)
    return score


# --------------------------------------------------------------------------
# Stage 2/3/4 — cross-section
# --------------------------------------------------------------------------


def _pct_rank(values: dict[str, Optional[float]]) -> dict[str, float]:
    """Percentile rank in (0, 1]; missing values get a neutral 0.5."""
    present = {k: v for k, v in values.items() if v is not None and math.isfinite(v)}
    out = {k: 0.5 for k in values}
    if present:
        s = pd.Series(present).rank(pct=True, method="average")
        out.update(s.to_dict())
    return out


def market_regime(
    spy: Optional[pd.DataFrame], techs: dict[str, dict], cfg: FunnelConfig = FunnelConfig()
) -> tuple[bool, Optional[float]]:
    """Risk-on when SPY is above its 200-DMA AND > half the universe is
    above its own. Missing SPY data defaults to risk-off."""
    breadth = None
    if techs:
        breadth = float(np.mean([t["above_ma_slow"] for t in techs.values()]))
    if spy is None or len(spy) < cfg.ma_slow:
        return False, breadth
    close = spy["close"].astype(float)
    spy_ok = float(close.iloc[-1]) > float(close.tail(cfg.ma_slow).mean())
    return bool(spy_ok and breadth is not None and breadth > cfg.breadth_risk_on), breadth


def trade_plan(tech: dict, cfg: FunnelConfig = FunnelConfig()) -> tuple[str, float, float]:
    """(entry note, stop, target) from the as-of close."""
    px = tech["price"]
    stop = px - cfg.stop_atr_mult * tech["atr"]
    swing = tech.get("swing_low")
    if swing is not None and swing < px:
        stop = max(stop, swing * 0.99)
    stop = max(stop, px * (1 - cfg.max_risk_pct / 100))
    if stop >= px:
        stop = px * (1 - cfg.max_risk_pct / 100)
    risk = px - stop
    target = max(px * (1 + cfg.target_min_pct / 100), px + cfg.target_r_multiple * risk)
    note = (
        f"Buy-stop above 10d high ${tech['high_10d']:.2f}, "
        f"or limit near 21-EMA ${tech['ema_21']:.2f} on a pullback"
    )
    return note, round(stop, 2), round(target, 2)


def _ordinal_top(p: float) -> str:
    return f"top {max(1, int(round((1 - p) * 100)))}%"


def _thesis(lane: str, sector: Optional[str], tech: dict, earn: dict, pcts: dict, rs_vs_sector: Optional[float], rec_pts: Optional[int]) -> str:
    parts = []
    if lane == "momentum":
        rs = f"{_ordinal_top(pcts.get('A', 0.5))} 6m RS ({tech['ret_6m'] * 100:+.0f}%"
        if rs_vs_sector is not None:
            rs += f", {rs_vs_sector * 100:+.0f}% vs {sector or 'sector'}"
        parts.append(rs + ")")
        parts.append(f"{abs(tech['drawdown_pct']):.0f}% off 52w high")
    else:
        parts.append(f"Quality name {abs(tech['drawdown_pct']):.0f}% off highs, back over 50-DMA")
        if rec_pts is not None:
            parts.append(f"base score {rec_pts}")
    if earn.get("eps_surprise") is not None:
        e = f"EPS {earn['eps_surprise'] * 100:+.0f}% vs est"
        if earn.get("drift") is not None:
            e += f", {earn['drift'] * 100:+.0f}% since report"
        parts.append(e)
    return "; ".join(parts)


@dataclass
class CrossSection:
    """Per-date features for every ticker, computed once and shared by all
    selection variants (the backtest's ablations reuse one of these)."""

    as_of: date
    n_inputs: int
    techs: dict[str, dict]
    frames: dict[str, pd.DataFrame]
    sectors: dict[str, Optional[str]]
    earns: dict[str, dict]
    quality_ok: set
    liquid_ok: set
    risk_on: bool
    breadth: Optional[float]


def bars_needed(cfg: FunnelConfig) -> int:
    """Bars handed to the factor code. Fixed so live and backtest see the
    same window (EMA/ATR seeds depend on where the series starts)."""
    return max(
        cfg.ma_slow + cfg.ma_slow_slope_lookback,
        cfg.high_lookback,
        cfg.rs_lookback + 1,
        cfg.hv_long + 1,
    ) + 1


def prepare_cross_section(
    inputs: list[TickerInput],
    as_of: date,
    spy: Optional[pd.DataFrame] = None,
    cfg: FunnelConfig = FunnelConfig(),
) -> CrossSection:
    techs: dict[str, dict] = {}
    frames: dict[str, pd.DataFrame] = {}
    sectors: dict[str, Optional[str]] = {}
    earns: dict[str, dict] = {}
    quality_ok: set = set()
    liquid_ok: set = set()
    n_bars = bars_needed(cfg)

    for ti in inputs:
        full = bars_as_of(ti.bars, as_of)
        if full is None or full.empty:
            continue
        df = full.tail(n_bars)
        tech = compute_technicals(df, cfg)
        if tech is None:
            continue
        techs[ti.ticker] = tech
        # Recovery metrics see the live scanner's 252-bar window.
        frames[ti.ticker] = full.tail(252)
        sectors[ti.ticker] = ti.sector
        raw_px = tech["raw_price"]
        if raw_px >= cfg.min_price and tech["dollar_volume"] >= cfg.min_dollar_volume:
            liquid_ok.add(ti.ticker)
            earns[ti.ticker] = compute_earnings_factors(ti.earnings, full, as_of, cfg)
            qm = compute_quality_metrics(
                ti.income, ti.balance, ti.cashflow, as_of, tech["price"], cfg, market_cap=tech["market_cap"]
            )
            if passes_quality_gate(qm, ti.sector, raw_px, tech["dollar_volume"], cfg)[0]:
                quality_ok.add(ti.ticker)

    risk_on, breadth = market_regime(bars_as_of(spy, as_of) if spy is not None else None, techs, cfg)
    return CrossSection(as_of, len(inputs), techs, frames, sectors, earns, quality_ok, liquid_ok, risk_on, breadth)


def run_funnel(
    inputs: list[TickerInput],
    as_of: date,
    spy: Optional[pd.DataFrame] = None,
    cfg: FunnelConfig = FunnelConfig(),
    factors: tuple[str, ...] = FACTORS,
    use_quality_gate: bool = True,
    use_risk_screen: bool = True,
    use_recovery: bool = True,
) -> FunnelResult:
    """Run all four stages for one date. Switches exist for ablations."""
    cs = prepare_cross_section(inputs, as_of, spy, cfg)
    return select_picks(cs, cfg, factors, use_quality_gate, use_risk_screen, use_recovery)


def select_picks(
    cs: CrossSection,
    cfg: FunnelConfig = FunnelConfig(),
    factors: tuple[str, ...] = FACTORS,
    use_quality_gate: bool = True,
    use_risk_screen: bool = True,
    use_recovery: bool = True,
) -> FunnelResult:
    """Stages 2-4 over a prepared cross-section."""
    as_of, techs, frames, sectors = cs.as_of, cs.techs, cs.frames, cs.sectors
    gate = cs.quality_ok if use_quality_gate else cs.liquid_ok
    passers = sorted(gate)
    earns = cs.earns
    risk_on, breadth = cs.risk_on, cs.breadth
    counts = {"universe": cs.n_inputs, "with_history": len(techs), "quality": len(passers)}

    # --- Factor percentiles across all quality passers ---
    ret6 = {t: techs[t]["ret_6m"] for t in passers}
    by_sector: dict[Optional[str], list[float]] = {}
    for t in passers:
        by_sector.setdefault(sectors[t], []).append(ret6[t])
    sector_med = {s: float(np.median(v)) for s, v in by_sector.items()}
    rs_rel = {t: ret6[t] - sector_med[sectors[t]] for t in passers}
    p_abs, p_rel = _pct_rank(ret6), _pct_rank(rs_rel)
    p_a = {t: 0.5 * p_abs[t] + 0.5 * p_rel[t] for t in passers}
    p_b = _pct_rank({t: techs[t]["prox_high"] for t in passers})
    pe = _pct_rank({t: earns[t]["eps_surprise"] for t in passers})
    pr = _pct_rank({t: earns[t]["rev_surprise"] for t in passers})
    pdft = _pct_rank({t: earns[t]["drift"] for t in passers})
    p_c = {t: (pe[t] + pr[t] + pdft[t]) / 3 for t in passers}
    ph = _pct_rank({t: -techs[t]["hv_ratio"] for t in passers})
    pu = _pct_rank({t: techs[t]["updown_vol"] for t in passers})
    p_d = {t: (ph[t] + pu[t]) / 2 for t in passers}
    factor_maps = {"A": p_a, "B": p_b, "C": p_c}

    # --- Momentum lane ---
    momentum = [t for t in passers if techs[t]["trend_ok"]]
    composite = {
        t: float(np.mean([factor_maps[f][t] for f in factors])) if factors else 0.5
        for t in momentum
    }
    comp_pct = _pct_rank(composite)
    momentum.sort(key=lambda t: (composite[t], p_d[t]), reverse=True)
    counts["momentum_eligible"] = len(momentum)

    def tier_of(t: str) -> Optional[str]:
        if comp_pct[t] >= cfg.tier_a:
            return "A"
        if comp_pct[t] >= cfg.tier_b:
            return "B"
        return None

    mom_ranked = [(t, tier_of(t)) for t in momentum if tier_of(t)]

    # --- Recovery lane ---
    rec_ranked: list[tuple[str, int]] = []
    rec_pts_map: dict[str, Optional[int]] = {}
    if use_recovery:
        for t in passers:
            tech, e = techs[t], earns[t]
            if (
                tech["drawdown_pct"] <= -cfg.recovery_drawdown_pct
                and tech["above_ma_fast"]
                and e["eps_surprise"] is not None
                and e["eps_surprise"] > 0
            ):
                pts = recovery_points(frames[t], tech)
                rec_pts_map[t] = pts
                rec_ranked.append((t, pts if pts is not None else -999))
        rec_ranked.sort(key=lambda x: (x[1], p_c[x[0]]), reverse=True)
    counts["recovery_eligible"] = len(rec_ranked)

    # --- Stage 3: risk screen + selection ---
    def screened_out(t: str) -> bool:
        if not use_risk_screen:
            return False
        ne = earns[t]["next_earnings"]
        if ne is not None and (ne - as_of).days <= cfg.earnings_blackout_days:
            return True
        return techs[t]["atr_pct"] > cfg.max_atr_pct

    n_target = cfg.n_risk_on if (risk_on or not use_risk_screen) else cfg.n_risk_off
    allowed_tiers = {"A", "B"} if (risk_on or not use_risk_screen) else {"A"}
    rec_reserve = min(cfg.recovery_slots, n_target // 3) if use_recovery and "B" in allowed_tiers else 0

    chosen: list[tuple[str, str, str]] = []  # (ticker, lane, tier)
    sector_n: dict[Optional[str], int] = {}

    def fits(t: str) -> bool:
        if any(c[0] == t for c in chosen):
            return False
        if screened_out(t):
            return False
        if not use_risk_screen:
            return True
        if sector_n.get(sectors[t], 0) >= cfg.max_per_sector:
            return False
        r = techs[t]["returns"]
        for c in chosen:
            o = techs[c[0]]["returns"]
            m = min(len(r), len(o))
            if m > 10:
                a, b = r[-m:], o[-m:]
                ok = ~(np.isnan(a) | np.isnan(b))
                if ok.sum() > 10 and np.corrcoef(a[ok], b[ok])[0, 1] > cfg.max_pair_corr:
                    return False
        return True

    def take(t: str, lane: str, tier: str) -> None:
        chosen.append((t, lane, tier))
        sector_n[sectors[t]] = sector_n.get(sectors[t], 0) + 1

    mom_quota = n_target - rec_reserve
    for t, tier in mom_ranked:
        if len(chosen) >= mom_quota:
            break
        if tier in allowed_tiers and fits(t):
            take(t, "momentum", tier)
    if rec_reserve:
        n_rec = 0
        for t, _ in rec_ranked:
            if n_rec >= rec_reserve or len(chosen) >= n_target:
                break
            if fits(t):
                take(t, "recovery", "B")
                n_rec += 1
    for t, tier in mom_ranked:  # backfill unused recovery slots
        if len(chosen) >= n_target:
            break
        if tier in allowed_tiers and fits(t):
            take(t, "momentum", tier)
    counts["picks"] = len(chosen)

    picks = []
    for t, lane, tier in chosen:
        tech = techs[t]
        note, stop, target = trade_plan(tech, cfg)
        pcts = {f: round(factor_maps[f][t], 3) for f in FACTORS}
        pcts["D"] = round(p_d[t], 3)
        picks.append(
            Pick(
                ticker=t,
                sector=sectors[t],
                lane=lane,
                tier=tier,
                composite=round(composite.get(t, p_c[t]), 3),
                factor_pcts=pcts,
                thesis=_thesis(lane, sectors[t], tech, earns[t], pcts, rs_rel.get(t), rec_pts_map.get(t)),
                price=round(tech["price"], 2),
                entry_note=note,
                stop=stop,
                target=target,
                next_earnings=earns[t]["next_earnings"],
            )
        )
    return FunnelResult(
        as_of=as_of,
        risk_on=risk_on,
        breadth=breadth,
        stage_counts=counts,
        picks=picks,
        quality_passers=passers,
    )
