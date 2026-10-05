"""Funnel stages (app/services/funnel.py) on synthetic and hand-built inputs."""

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services.funnel import (
    FunnelConfig,
    TickerInput,
    compute_earnings_factors,
    compute_quality_metrics,
    compute_technicals,
    passes_quality_gate,
    prepare_cross_section,
    run_funnel,
    statement_available_date,
    statements_as_of,
    trade_plan,
)
from scripts.backtest.synthetic import make_market, make_statements

AS_OF = date(2019, 6, 28)


@pytest.fixture(scope="module")
def market():
    return make_market(n_tickers=48, end=date(2020, 6, 30))


def _inputs(market, bars_override=None):
    out = []
    for sym, d in market["tickers"].items():
        bars = bars_override[sym] if bars_override else d["bars"]
        out.append(TickerInput(sym, d["sector"], bars, d["income"], d["balance"], d["cashflow"], d["earnings"]))
    return out


# ---- point-in-time statements ------------------------------------------


def test_available_date_prefers_filing_date():
    assert statement_available_date({"date": "2020-03-31", "filingDate": "2020-05-01"}) == date(2020, 5, 1)


def test_available_date_falls_back_to_period_end_plus_lag():
    assert statement_available_date({"date": "2020-03-31"}, 45) == date(2020, 5, 15)


def test_statement_filed_on_as_of_is_not_yet_known():
    stmts = [
        {"date": "2020-03-31", "filingDate": "2020-05-01"},
        {"date": "2019-12-31", "filingDate": "2020-02-10"},
    ]
    got = statements_as_of(stmts, date(2020, 5, 1))
    assert [s["date"] for s in got] == ["2019-12-31"]


# ---- quality gate ---------------------------------------------------------


def _stmts(quality: bool):
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2017-01-01", "2020-12-31")
    return make_statements(rng, days, quality, shares=1e8)


def test_quality_company_passes():
    inc, bal, cf = _stmts(True)
    m = compute_quality_metrics(inc, bal, cf, date(2020, 6, 1), price=100.0)
    ok, fails = passes_quality_gate(m, "Industrials", 100.0, 50e6)
    assert ok, fails


def test_dumpster_fails_on_fundamentals():
    inc, bal, cf = _stmts(False)
    m = compute_quality_metrics(inc, bal, cf, date(2020, 6, 1), price=100.0)
    ok, fails = passes_quality_gate(m, "Industrials", 100.0, 50e6)
    assert not ok
    assert {"free cash flow", "revenue shrinking", "leverage"} <= set(fails)


def test_financials_judged_on_net_income_not_fcf_or_leverage():
    inc, bal, cf = _stmts(True)
    for c in cf:
        c["freeCashFlow"] = -1e9  # meaningless for a bank
    for b in bal:
        b["totalDebt"] = 1e12
    m = compute_quality_metrics(inc, bal, cf, date(2020, 6, 1), price=100.0)
    ok, fails = passes_quality_gate(m, "Financial Services", 100.0, 50e6)
    assert ok, fails


def test_missing_fundamentals_fail_the_gate():
    ok, fails = passes_quality_gate(None, "Technology", 100.0, 50e6)
    assert not ok and "no fundamentals" in fails


def test_illiquid_and_cheap_fail():
    inc, bal, cf = _stmts(True)
    m = compute_quality_metrics(inc, bal, cf, date(2020, 6, 1), price=5.0)
    ok, fails = passes_quality_gate(m, "Industrials", 5.0, 1e6)
    assert not ok and {"price", "liquidity"} <= set(fails)


def test_dilution_measured_year_over_year():
    inc, bal, cf = _stmts(True)
    for s in inc[:1]:
        s["weightedAverageShsOutDil"] = 1.2e8  # +20% vs four quarters ago
    m = compute_quality_metrics(inc, bal, cf, date(2021, 3, 1), price=100.0)
    assert m.dilution_pct == pytest.approx(20.0)
    assert "dilution" in passes_quality_gate(m, "Industrials", 100.0, 50e6)[1]


# ---- technicals / earnings -----------------------------------------------


def _trend_bars(n=300, step=0.002):
    close = 100 * np.exp(np.arange(n) * step)
    return pd.DataFrame({
        "date": pd.bdate_range("2018-01-01", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": np.full(n, 1e6),
    })


def test_trend_template_passes_steady_uptrend_and_fails_downtrend():
    assert compute_technicals(_trend_bars(step=0.002))["trend_ok"] is True
    assert compute_technicals(_trend_bars(step=-0.002))["trend_ok"] is False


def test_technicals_need_enough_history():
    assert compute_technicals(_trend_bars(n=150)) is None


def _later_split(df: pd.DataFrame, k: float = 40.0) -> pd.DataFrame:
    """As a backtest sees a name that splits k:1 after the window: adjusted
    prices and volumes scaled, the as-traded tape alongside."""
    out = df.copy()
    out["raw_close"], out["raw_volume"] = out["close"], out["volume"]
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] / k
    out["volume"] = out["volume"] * k
    return out


def test_dollar_thresholds_use_the_unadjusted_tape():
    # ~$180 as traded, ~$4.50 split-adjusted: a $10 floor on adjusted
    # prices would drop it using knowledge of its future split.
    tech = compute_technicals(_later_split(_trend_bars()))
    assert tech["price"] < 10 < tech["raw_price"]
    assert tech["dollar_volume"] == pytest.approx(compute_technicals(_trend_bars())["dollar_volume"])
    ti = TickerInput("X", "Industrials", _later_split(_trend_bars()))
    assert "X" in prepare_cross_section([ti], date(2019, 12, 31)).liquid_ok


def test_raw_columns_absent_fall_back_to_close():
    tech = compute_technicals(_trend_bars())
    assert tech["raw_price"] == tech["price"] and tech["market_cap"] is None


def test_historical_market_cap_overrides_price_times_shares():
    df = _trend_bars()
    df["market_cap"] = np.nan
    df.loc[len(df) - 3, "market_cap"] = 7.5e9  # last known value carries
    assert compute_technicals(df)["market_cap"] == 7.5e9
    inc, bal, cf = _stmts(True)
    m = compute_quality_metrics(inc, bal, cf, date(2020, 6, 1), price=100.0, market_cap=7.5e9)
    assert m.market_cap == 7.5e9


def test_earnings_on_as_of_day_is_not_in_the_signal():
    bars = _trend_bars()
    earnings = [
        {"date": "2019-01-10", "epsActual": 1.2, "epsEstimated": 1.0},
        {"date": "2019-01-03", "epsActual": 0.5, "epsEstimated": 1.0},
    ]
    e = compute_earnings_factors(earnings, bars, date(2019, 1, 10))
    assert e["report_date"] == date(2019, 1, 3)
    assert e["eps_surprise"] == pytest.approx(-0.5)
    assert e["next_earnings"] == date(2019, 1, 10)


def test_stale_report_is_neutral():
    e = compute_earnings_factors(
        [{"date": "2018-01-02", "epsActual": 2.0, "epsEstimated": 1.0}], _trend_bars(), date(2019, 1, 10)
    )
    assert e["eps_surprise"] is None


def test_trade_plan_caps_risk_and_floors_target():
    tech = compute_technicals(_trend_bars())
    tech = {**tech, "atr": tech["price"] * 0.2, "swing_low": None}  # absurd ATR
    _, stop, target = trade_plan(tech)
    assert stop >= tech["price"] * 0.9 - 0.01
    assert target >= tech["price"] * 1.15 - 0.01


# ---- full funnel -----------------------------------------------------------


def test_funnel_respects_size_sector_and_quality(market):
    cfg = FunnelConfig()
    res = run_funnel(_inputs(market), AS_OF, market["spy"], cfg)
    n_cap = cfg.n_risk_on if res.risk_on else cfg.n_risk_off
    assert 0 < len(res.picks) <= n_cap
    by_sector = pd.Series([p.sector for p in res.picks]).value_counts()
    assert by_sector.max() <= cfg.max_per_sector
    assert all(p.ticker in res.quality_passers for p in res.picks)
    for p in res.picks:
        assert p.stop < p.price < p.target
        assert p.thesis


def test_risk_off_shrinks_list_to_a_tier(market):
    spy = market["spy"].copy()
    spy["close"] = np.linspace(400, 100, len(spy))  # SPY far below 200-DMA
    res = run_funnel(_inputs(market), AS_OF, spy, FunnelConfig())
    assert res.risk_on is False
    assert len(res.picks) <= FunnelConfig().n_risk_off
    assert all(p.tier == "A" for p in res.picks)


def test_no_look_ahead_future_bars_and_reports_cannot_change_picks(market):
    """Scramble everything after AS_OF: picks must be identical."""
    base = run_funnel(_inputs(market), AS_OF, market["spy"])
    rng = np.random.default_rng(99)
    scrambled = {}
    for sym, d in market["tickers"].items():
        b = d["bars"].copy()
        fut = pd.to_datetime(b["date"]) > pd.Timestamp(AS_OF)
        for col in ("open", "high", "low", "close"):
            b.loc[fut, col] = b.loc[fut, col] * rng.uniform(0.3, 3.0, fut.sum())
        scrambled[sym] = b
    inputs = _inputs(market, scrambled)
    for ti in inputs:
        ti.earnings = [
            {**e, "epsActual": -99.0} if date.fromisoformat(e["date"]) >= AS_OF else e
            for e in ti.earnings
        ]
        ti.income = [
            {**s, "revenue": 1.0} if date.fromisoformat(s["filingDate"]) >= AS_OF else s
            for s in ti.income
        ]
    spy = market["spy"].copy()
    spy.loc[pd.to_datetime(spy["date"]) > pd.Timestamp(AS_OF), "close"] = 1.0
    again = run_funnel(inputs, AS_OF, spy)
    assert [(p.ticker, p.tier, p.composite) for p in again.picks] == [
        (p.ticker, p.tier, p.composite) for p in base.picks
    ]


def test_ablation_switches_change_the_pipeline(market):
    full = run_funnel(_inputs(market), AS_OF, market["spy"])
    no_gate = run_funnel(_inputs(market), AS_OF, market["spy"], use_quality_gate=False)
    assert len(no_gate.quality_passers) > len(full.quality_passers)
    only_a = run_funnel(_inputs(market), AS_OF, market["spy"], factors=("A",))
    assert only_a.stage_counts["momentum_eligible"] == full.stage_counts["momentum_eligible"]


def test_scaled_config_changes_lookbacks_only():
    cfg = FunnelConfig().scaled(1.2)
    assert cfg.rs_lookback == round(126 * 1.2)
    assert replace(cfg, rs_lookback=126).max_pair_corr == FunnelConfig().max_pair_corr
