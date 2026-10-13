"""Backtest harness (scripts/backtest): membership, outcomes, stats, e2e."""

import gzip
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from scripts.backtest import data as D
from scripts.backtest import metrics as M
from scripts.backtest.engine import Engine, EngineConfig, rebalance_dates, run_backtest
from scripts.backtest.report import build_report


def test_membership_walks_back_through_changes():
    events = [
        {"date": "2020-06-01", "symbol": "C", "removedTicker": "D"},
        {"date": "2019-01-02", "symbol": "B", "removedTicker": "E"},
        {"date": "2010-01-04", "symbol": "A", "removedTicker": "Z"},  # before start: ignored
    ]
    tl = D.build_membership(["A", "B", "C"], events, start=date(2015, 1, 1))
    ds = D.Dataset({}, pd.DataFrame(), tl, "t", False)
    assert ds.members_at(date(2018, 6, 1)) == {"A", "D", "E"}
    assert ds.members_at(date(2019, 1, 2)) == {"A", "B", "D"}
    assert ds.members_at(date(2020, 5, 29)) == {"A", "B", "D"}
    assert ds.members_at(date(2020, 6, 1)) == {"A", "B", "C"}
    assert ds.members_at(date(2026, 1, 1)) == {"A", "B", "C"}


def test_same_day_events_all_apply():
    events = [
        {"date": "2020-06-01", "symbol": "C", "removedTicker": "D"},
        {"date": "2020-06-01", "symbol": "F", "removedTicker": "G"},
    ]
    tl = D.build_membership(["A", "C", "F"], events, start=date(2015, 1, 1))
    ds = D.Dataset({}, pd.DataFrame(), tl, "t", False)
    assert ds.members_at(date(2020, 6, 1)) == {"A", "C", "F"}
    assert ds.members_at(date(2020, 5, 29)) == {"A", "D", "G"}


def _spy(n=200):
    days = pd.bdate_range("2020-01-01", periods=n)
    px = np.linspace(100, 120, n)
    return pd.DataFrame({"date": days, "open": px, "high": px, "low": px, "close": px, "volume": 1.0})


def test_rebalance_dates_are_week_ends_with_full_horizon():
    spy = _spy()
    ds = rebalance_dates(spy, date(2020, 1, 1), date(2021, 1, 1), "W", 63)
    assert all(pd.Timestamp(d).dayofweek == 4 for d in ds[:-1])
    last_allowed = pd.to_datetime(spy["date"]).iloc[-1 - 63].date()
    assert ds[-1] <= last_allowed


def _engine_with(bars: pd.DataFrame, horizon=5):
    ds = D.Dataset({"X": {"sector": "T", "bars": bars, "income": [], "balance": [], "cashflow": [], "earnings": []}},
                   bars.copy(), [(date.min, frozenset({"X"}))], "t", False)
    return Engine(ds, ecfg=EngineConfig(horizon=horizon, cost_bps=0))


def _bars(opens, highs, lows, closes):
    n = len(closes)
    return pd.DataFrame({"date": pd.bdate_range("2021-01-04", periods=n), "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": 1e6})


def test_outcome_enters_next_open_and_exits_at_horizon_close():
    c = [100, 100, 104, 106, 108, 110, 112, 130]
    e = _engine_with(_bars([100, 101, 103, 105, 107, 109, 111, 129], [x + 1 for x in c], [x - 1 for x in c], c))
    o = e.outcome("X", date(2021, 1, 4), 0.5, 10.0)  # signal day 0 -> entry open of day 1 (101)
    assert o["ret"] == pytest.approx(110 / 101 - 1)
    assert o["truncated"] is False
    assert o["daily"].shape == (5,)
    assert np.prod(1 + o["daily"]) - 1 == pytest.approx(o["ret"])


def test_outcome_flags_truncated_holds():
    c = [100, 100, 101]
    e = _engine_with(_bars(c, c, c, c))
    o = e.outcome("X", date(2021, 1, 4), 0.5, 10.0)
    assert o["truncated"] is True and o["ret"] == pytest.approx(101 / 100 - 1)


def test_managed_exit_hits_stop_and_never_counts_a_later_rally():
    o_ = [100, 100, 95, 120]
    h_ = [101, 101, 96, 125]
    l_ = [99, 99, 89, 119]
    c_ = [100, 100, 90, 124]
    e = _engine_with(_bars(o_, h_, l_, c_), horizon=3)
    o = e.outcome("X", date(2021, 1, 4), 0.9, 1.15)
    assert o["managed_ret"] == pytest.approx(-0.10)
    assert o["touched10_first"] is False
    assert o["ret"] == pytest.approx(0.24)


def test_newey_west_zero_lags_is_plain_t():
    x = np.array([0.01, 0.03, -0.02, 0.05, 0.00, 0.02])
    mu, t = M.newey_west_t(x, 0)
    assert mu == pytest.approx(x.mean())
    assert t == pytest.approx(x.mean() / (x.std(ddof=0) / np.sqrt(len(x))))


@pytest.fixture(scope="module")
def small_run():
    ds = D.synthetic_dataset(n_tickers=30, start=date(2014, 1, 1), end=date(2018, 12, 31))
    eng = Engine(ds, ecfg=EngineConfig(random_draws=3))
    dates = rebalance_dates(ds.spy, date(2016, 1, 1), date(2018, 6, 30), "M", 63)
    rows, diags = run_backtest(eng, dates, workers=1)
    return ds, eng, dates, rows, diags


def test_end_to_end_report(small_run):
    ds, _, dates, rows, diags = small_run
    assert {"funnel", "scanner_top15", "random_quality_gate", "universe_equal_weight", "spy"} <= set(rows["variant"])
    assert (rows[rows["variant"] == "spy"].groupby("date").size() == 1).all()
    md, dec = build_report(rows, diags, ds, {
        "run_label": "test", "synthetic": True, "horizon": 63, "cost_bps": 10, "nw_lags": 3,
        "oos_start": date(2017, 6, 1), "freq_label": "monthly",
    })
    assert "SYNTHETIC DATA" in md and "## Verdict" in md
    assert set(dec["comparisons"]) == {"scanner_top15", "random_quality_gate"}


def test_point_in_time_universe_drops_removed_member(small_run):
    ds, _, _, rows, _ = small_run
    removed_on = ds.membership[1][0]
    late = rows[(rows["date"] >= removed_on) & (rows["variant"] == "universe_equal_weight")]
    assert "T001" not in set(late["ticker"])


def test_engine_picks_ignore_the_future(small_run):
    """Scramble every bar after a signal date: that date's picks can't move."""
    ds, eng, dates, rows, _ = small_run
    d = dates[len(dates) // 2]
    rng = np.random.default_rng(5)
    scrambled = {}
    for sym, t in ds.tickers.items():
        b = t["bars"].copy()
        fut = pd.to_datetime(b["date"]) > pd.Timestamp(d)
        for col in ("open", "high", "low", "close"):
            b.loc[fut, col] *= rng.uniform(0.2, 5.0, fut.sum())
        scrambled[sym] = {**t, "bars": b}
    ds2 = D.Dataset(scrambled, ds.spy, ds.membership, ds.universe_label, False, ds.coverage)
    rows2, _ = Engine(ds2, ecfg=EngineConfig(random_draws=3)).run_date(d)
    picked = lambda rs: sorted((r["variant"], r["ticker"]) for r in rs if r["variant"] not in ("universe_equal_weight", "spy"))  # noqa: E731
    base = rows[rows["date"] == d].to_dict("records")
    assert picked(rows2) == picked(base)


class _StubFetcher(D.FMPFetcher):
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, endpoint, refresh=False, **params):
        return self.payloads.get(endpoint, [])


def test_bars_fall_back_when_adjusted_series_lacks_ohlc():
    close_only = [{"date": "2020-01-02", "adjClose": 10.0, "volume": 5}]
    full = [{"date": "2020-01-02", "open": 9, "high": 11, "low": 8, "close": 10, "volume": 5}]
    f = _StubFetcher({"/stable/historical-price-eod/dividend-adjusted": close_only,
                      "/stable/historical-price-eod/full": full})
    assert f.bars("X", date(2020, 1, 1), date(2020, 2, 1)) == full


def test_bars_prefer_adjusted_ohlc():
    adj = [{"date": "2020-01-02", "adjOpen": 9, "adjHigh": 11, "adjLow": 8, "adjClose": 10, "volume": 5}]
    f = _StubFetcher({"/stable/historical-price-eod/dividend-adjusted": adj})
    assert f.bars("X", date(2020, 1, 1), date(2020, 2, 1))[0]["close"] == 10


def test_unadjusted_tape_and_market_cap_attach_by_date():
    adj = [{"date": f"2020-01-0{i}", "adjOpen": 1, "adjHigh": 1, "adjLow": 1, "adjClose": 1, "volume": 40} for i in (2, 3, 6)]
    raw = [{"date": "2020-01-02", "adjClose": 40.0, "volume": 1}, {"date": "2020-01-03", "adjClose": 41.0, "volume": 1}]
    caps = [{"date": "2020-01-02", "marketCap": 5e9}]
    f = _StubFetcher({"/stable/historical-price-eod/dividend-adjusted": adj,
                      "/stable/historical-price-eod/non-split-adjusted": raw,
                      "/stable/historical-market-capitalization": caps})
    s, e = date(2020, 1, 1), date(2020, 2, 1)
    df = D.attach_unadjusted(D._frame(f.bars("X", s, e)), f.raw_bars("X", s, e), f.market_caps("X", s, e))
    assert df["raw_close"].tolist()[:2] == [40.0, 41.0] and pd.isna(df["raw_close"].iloc[2])
    assert df["market_cap"].tolist() == [5e9, 5e9, 5e9]  # carried forward, within 10 days


def test_attach_leaves_off_columns_with_no_data():
    bars = D._frame([{"date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    assert list(D.attach_unadjusted(bars, [], [])) == list(bars)


def test_scanner_replay_stage1_sees_the_unadjusted_tape(small_run):
    """Scale every adjusted price under the scanner's $20 floor (as a later
    40:1 split would) while the as-traded tape stays put: same picks."""
    ds, eng, dates, _, _ = small_run
    d = dates[len(dates) // 2]
    split = {}
    for sym, t in ds.tickers.items():
        b = t["bars"].copy()
        b["raw_close"], b["raw_volume"] = b["close"], b["volume"]
        for c in ("open", "high", "low", "close"):
            b[c] = b[c] / 40
        b["volume"] = b["volume"] * 40
        # FMP split-adjusts historical share counts too (verified 2026-10-05).
        inc = [{**s, "weightedAverageShsOutDil": s["weightedAverageShsOutDil"] * 40} for s in t["income"]]
        split[sym] = {**t, "bars": b, "income": inc}
    eng2 = Engine(D.Dataset(split, ds.spy, ds.membership, ds.universe_label, False, ds.coverage), ecfg=EngineConfig(random_draws=3))
    base = eng.scanner_top(eng.inputs_at(d), d)
    assert base and eng2.scanner_top(eng2.inputs_at(d), d) == base


def test_parallel_workers_match_serial(small_run):
    # spawn on Windows, fork elsewhere: both must reproduce the serial rows.
    ds, eng, dates, rows, _ = small_run
    some = dates[:2]
    par, _ = run_backtest(eng, some, workers=2)
    ser = rows[rows["date"].isin(some)]
    key = lambda df: sorted(zip(df["variant"], df["date"], df["ticker"], df["ret"].round(12)))  # noqa: E731
    assert key(par) == key(ser)


def test_public_membership_starts_at_last_snapshot_before_start():
    csv = 'date,tickers\n2015-12-01,"AAA,BRK.B"\n2016-01-04,"AAA,BRK.B,CCC"\n2017-03-01,"BRK.B,CCC"\n'
    m = D.parse_public_membership(csv, date(2016, 1, 1))
    ds = D.Dataset({}, None, m, "t", False)
    assert ds.members_at(date(2016, 1, 1)) == {"AAA", "BRK-B"}
    assert ds.members_at(date(2016, 6, 1)) == {"AAA", "BRK-B", "CCC"}
    assert ds.members_at(date(2018, 1, 1)) == {"BRK-B", "CCC"}


def test_renamed_ticker_borrows_only_missing_pieces():
    f = _StubFetcher({})
    f.statements = lambda kind, s: [{"date": "2020-03-31", "who": s}] if s == "META" else []
    f.earnings = lambda s: [{"who": s}]
    assert D._with_alias(lambda s: f.statements("income-statement", s), "FB")[0]["who"] == "META"
    assert D._with_alias(f.earnings, "FB")[0]["who"] == "FB"  # FB's own data wins
    assert D._with_alias(lambda s: f.statements("income-statement", s), "XYZ") == []


def _reused_ticker_fetcher():
    """FB as FMP serves it since 2022: a different security's earnings (non-
    empty, 2023+), no prices; Facebook's history lives under META."""
    bars = [{"date": d.strftime("%Y-%m-%d"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            for d in pd.bdate_range("2014-01-01", "2023-12-29")]
    earn = {
        "FB": [{"date": "2023-03-15", "epsActual": 9.0, "who": "reused"},
               {"date": "2016-07-27", "epsActual": 1.0, "lastUpdated": "2016-07-28", "who": "FB-stale"}],
        "META": [{"date": "2016-07-27", "epsActual": 1.1, "epsEstimated": 1.0, "lastUpdated": "2023-01-01", "who": "Facebook"},
                 {"date": "2016-11-02", "epsActual": 1.2, "lastUpdated": "2023-01-01", "who": "Facebook"},
                 {"date": "2021-10-25", "epsActual": 3.2, "lastUpdated": "2023-01-01", "who": "Facebook"}],
    }
    f = _StubFetcher({})
    f.cache_only, f.misses = False, 0
    f.bars = lambda s, a, b: bars if s in ("META", "SPY") else []
    f.raw_bars = f.market_caps = lambda s, a, b: []
    f.statements = lambda kind, s: []
    f.earnings = lambda s: list(earn.get(s, []))
    f.sector = lambda s: "Communication Services"
    return f


def test_reused_old_ticker_still_loads_the_renamed_companys_earnings(monkeypatch):
    f = _reused_ticker_fetcher()
    membership = [(date.min, frozenset({"FB"})), (date(2022, 6, 9), frozenset({"META"}))]
    monkeypatch.setattr(D, "universe_symbols", lambda fetcher, mode, start: (membership, False))
    ds = D.load_dataset(f, "pit500public", date(2016, 1, 1), date(2023, 12, 29))

    fb = ds.tickers["FB"]["earnings"]
    by_date = {r["date"]: r["who"] for r in fb}
    # Facebook's pre-rename history loads even though FB itself isn't empty.
    assert by_date["2016-11-02"] == by_date["2021-10-25"] == "Facebook"
    # One row per report date; the most recently updated wins.
    assert by_date["2016-07-27"] == "Facebook" and len(fb) == 4
    # The reused ticker's row is present but dated after FB left the index,
    # so the per-date membership check drops it downstream.
    assert by_date["2023-03-15"] == "reused" and "FB" not in ds.members_at(date(2023, 3, 15))

    from app.services.funnel import compute_earnings_factors

    got = compute_earnings_factors(fb, ds.tickers["FB"]["bars"], date(2016, 8, 15))
    assert got["report_date"] == date(2016, 7, 27) and got["eps_surprise"] == pytest.approx(0.1)


def test_earnings_events_use_the_merged_rows(monkeypatch):
    from scripts.backtest.earnings_events import build_events

    f = _reused_ticker_fetcher()
    membership = [(date.min, frozenset({"FB"})), (date(2022, 6, 9), frozenset({"META"}))]
    monkeypatch.setattr(D, "universe_symbols", lambda fetcher, mode, start: (membership, False))
    ds = D.load_dataset(f, "pit500public", date(2016, 1, 1), date(2023, 12, 29))
    ev = build_events(ds, date(2016, 1, 1), date(2023, 12, 29))
    fb = ev[ev["ticker"] == "FB"]
    assert sorted(fb["report_date"].astype(str)) == ["2016-07-27", "2016-11-02", "2021-10-25"]
    assert fb[fb["report_date"] == date(2016, 7, 27)]["eps_actual"].item() == 1.1


def _cached_fetcher(tmp_path, cache_only, files):
    f = D.FMPFetcher("", tmp_path, cache_only=cache_only)
    for params, payload in files:
        with gzip.open(f._path("/stable/historical-price-eod/full", params), "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
    f.client = None  # any network call fails the test
    return f


def test_cache_from_an_earlier_fetch_day_is_trimmed_to_the_requested_end(tmp_path):
    rows = [{"date": "2026-10-01", "close": 1}, {"date": "2026-10-05", "close": 2}]
    f = _cached_fetcher(tmp_path, False, [({"symbol": "SPY", "from": "2014-01-01", "to": "2026-10-05"}, rows)])
    assert f.get("/stable/historical-price-eod/full", symbol="SPY", **{"from": "2014-01-01", "to": "2026-10-02"}) == rows[:1]


def test_cache_only_reuses_an_older_range_and_never_hits_the_network(tmp_path):
    rows = [{"date": "2026-10-05", "close": 2}]
    f = _cached_fetcher(tmp_path, True, [({"symbol": "SPY", "from": "2014-01-01", "to": "2026-10-05"}, rows)])
    assert f.get("/stable/historical-price-eod/full", symbol="SPY", **{"from": "2014-01-01", "to": "2026-10-08"}) == rows
    assert f.get("/stable/historical-price-eod/full", symbol="SPYX", **{"from": "2014-01-01", "to": "2026-10-08"}) == {"__error__": "cache miss"}
    assert f.misses == 1


def test_older_range_is_refetched_when_the_network_is_allowed(tmp_path):
    f = _cached_fetcher(tmp_path, False, [({"symbol": "SPY", "from": "2014-01-01", "to": "2026-10-05"}, [])])
    assert f._other_range("/stable/historical-price-eod/full", {"symbol": "SPY", "from": "2014-01-01", "to": "2026-10-08"}) is None
