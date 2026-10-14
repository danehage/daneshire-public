"""Scanner fact columns (#117).

The scanner is a sortable shortlist now, not a ranker: each row carries
facts (% off the 52-week high, last earnings reaction vs SPY, EPS surprise,
next report, sector, ...) and the legacy score stays as one more column.
FMP is always a fake.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.services.scanner import (
    StockScanner,
    earnings_reaction,
    eps_surprise_pct,
)


# ---- pure helpers ---------------------------------------------------------


def test_reaction_spans_close_before_to_session_after_the_report():
    closes = {"2026-07-28": 100.0, "2026-07-29": 104.0, "2026-07-30": 110.0, "2026-07-31": 90.0}
    spy = {"2026-07-28": 500.0, "2026-07-30": 505.0}
    # Report 07-29: close before = 07-28, first session after = 07-30.
    assert earnings_reaction(closes, spy, "2026-07-29") == (10.0, 9.0)


def test_reaction_without_spy_dates_keeps_raw_move():
    closes = {"2026-07-28": 100.0, "2026-07-30": 95.0}
    assert earnings_reaction(closes, {}, "2026-07-29") == (-5.0, None)


@pytest.mark.parametrize(
    "report",
    [None, "2026-07-31", "2026-07-01"],  # no report / no session after / none before
)
def test_reaction_is_none_when_window_incomplete(report):
    closes = {"2026-07-28": 100.0, "2026-07-30": 95.0, "2026-07-31": 96.0}
    assert earnings_reaction(closes, {}, report) == (None, None)


@pytest.mark.parametrize(
    "actual,estimate,expected",
    [(2.1, 2.0, 5.0), (0.9, 1.0, -10.0), (-0.5, -1.0, 50.0), (1.0, 0.0, None),
     (None, 1.0, None), (1.0, None, None)],
)
def test_eps_surprise_pct(actual, estimate, expected):
    assert eps_surprise_pct(actual, estimate) == expected


# ---- through the scanner ---------------------------------------------------


def _bars(n=260, start_price=100.0, step=0.1):
    end = date.today() - timedelta(days=1)
    days = [end - timedelta(days=i) for i in range(n)][::-1]
    out = []
    for i, d in enumerate(days):
        c = start_price + step * i
        out.append({"date": d.isoformat(), "open": c, "high": c + 1, "low": c - 1,
                    "close": c, "volume": 1_000_000})
    return out


class FakeFMP:
    def __init__(self, spy=True):
        self.stock = _bars()
        self.spy = _bars(start_price=500.0, step=0.0) if spy else None
        self.hist_calls: list[str] = []
        self.report = self.stock[-30]["date"]

    async def get_historical_data(self, ticker, days=252, use_cache=True, save_cache=True):
        self.hist_calls.append(ticker)
        return self.spy if ticker == "SPY" else self.stock

    async def get_earnings_info(self, ticker):
        return {
            "next_date": "2099-01-01",
            "days_until": 40,
            "last_date": self.report,
            "last_eps_actual": 2.1,
            "last_eps_estimate": 2.0,
        }

    async def get_batch_quotes(self, tickers, max_concurrent=10):
        return {t: {"price": 120.0, "volume": 1_000_000, "marketCap": 5e9} for t in tickers}


async def test_scan_rows_carry_the_fact_columns(tmp_path):
    fmp = FakeFMP()
    scanner = StockScanner(cache_dir=str(tmp_path), client=fmp)

    _, results = await scanner.run_scan(["ZQF1"], use_cache=False)

    (row,) = results
    high = max(b["high"] for b in fmp.stock[-252:])
    assert row["pct_off_high"] == pytest.approx((120.0 / high - 1) * 100, abs=0.01)
    assert row["earnings_date"] == "2099-01-01" and row["days_to_earnings"] == 40
    assert row["last_earnings_date"] == fmp.report
    assert row["eps_surprise_pct"] == 5.0
    # Bars rise 0.1/day over a flat SPY: the 2-session move is the stock's own.
    i = next(k for k, b in enumerate(fmp.stock) if b["date"] == fmp.report)
    expected = round((fmp.stock[i + 1]["close"] / fmp.stock[i - 1]["close"] - 1) * 100, 2)
    assert row["earnings_reaction_pct"] == expected
    assert row["earnings_reaction_vs_spy"] == expected
    assert "score" in row  # the legacy score is still computed and shown


async def test_benchmark_is_fetched_once_per_day(tmp_path):
    fmp = FakeFMP()
    scanner = StockScanner(cache_dir=str(tmp_path), client=fmp)

    await scanner.run_scan(["ZQF1", "ZQF2"], use_cache=False)
    await scanner.run_scan(["ZQF1"], use_cache=False)

    assert fmp.hist_calls.count("SPY") == 1


async def test_missing_benchmark_leaves_vs_spy_empty(tmp_path):
    fmp = FakeFMP(spy=False)
    scanner = StockScanner(cache_dir=str(tmp_path), client=fmp)

    _, (row,) = await scanner.run_scan(["ZQF1"], use_cache=False)

    assert row["earnings_reaction_pct"] is not None
    assert row["earnings_reaction_vs_spy"] is None


def test_price_above_the_bar_high_counts_as_at_the_high(tmp_path):
    scanner = StockScanner(cache_dir=str(tmp_path), client=FakeFMP())
    df = pd.DataFrame(_bars())
    row = scanner._analyze_frame("ZQF1", {"price": 1000.0, "volume": 1_000_000}, df)
    assert row["pct_off_high"] == 0.0
