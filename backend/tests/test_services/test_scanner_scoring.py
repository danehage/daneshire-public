"""
Fixture tests for the recovery-track rescore (issue #49).

The five tickers are real trades: ESTC/EOG/APD were bought near their lows
and won (+23.9%, +26.6%, +23.6%); ADBE was bought on a fading bounce and
lost (-32.6%); AMC is the falling-knife control that the OLD score ranked at
or above all three winners. Fixture bars are frozen FMP daily history,
trimmed to each buy date, so these fingerprints can never drift.

Acceptance (from the #49 design comment): the three winners score high, AMC
and ADBE score <= 0, ADBE gets the "Fading bounce" label. Ordering is the
criterion, not exact point values.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from app.services.indicators import TechnicalIndicators
from app.services.scanner import StockScanner

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "scanner"

# (ticker, buy date, approx market cap at the buy date)
WINNERS = [
    ("ESTC", "2026-04-15", 5_000_000_000),
    ("EOG", "2026-01-02", 65_000_000_000),
    ("APD", "2026-01-02", 58_000_000_000),
]
LOSERS = [
    ("AMC", "2026-01-02", 1_500_000_000),
    ("ADBE", "2026-01-12", 140_000_000_000),
]


def _score_fixture(ticker: str, as_of: str, market_cap: int, cache_dir: str) -> dict:
    """Run one frozen fixture through the real analyze + score path."""
    with open(FIXTURES / f"{ticker}.json") as f:
        rows = json.load(f)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = (
        df[df["date"] <= pd.Timestamp(as_of)]
        .sort_values("date")
        .tail(252)
        .reset_index(drop=True)
    )

    scanner = StockScanner(api_key="test-key", cache_dir=cache_dir)
    quote = {
        "price": float(df["close"].iloc[-1]),
        "volume": int(df["volume"].iloc[-1]),
        "marketCap": market_cap,
    }
    result = scanner._analyze_frame(ticker, quote, df)
    assert result is not None, f"{ticker}: analysis returned None"

    score, signals, setup = scanner._score_opportunity(result)
    result["score"] = score
    result["signals"] = signals
    result["setup"] = setup
    return result


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    cache_dir = str(tmp_path_factory.mktemp("scanner_cache"))
    return {
        ticker: _score_fixture(ticker, as_of, mcap, cache_dir)
        for ticker, as_of, mcap in WINNERS + LOSERS
    }


def test_winners_score_high(scored):
    for ticker, _, _ in WINNERS:
        assert scored[ticker]["score"] >= 40, (
            f"{ticker} scored {scored[ticker]['score']}, expected >= 40"
        )


def test_losers_score_nonpositive(scored):
    for ticker, _, _ in LOSERS:
        assert scored[ticker]["score"] <= 0, (
            f"{ticker} scored {scored[ticker]['score']}, expected <= 0"
        )


def test_every_winner_ranks_above_every_loser(scored):
    worst_winner = min(scored[t]["score"] for t, _, _ in WINNERS)
    best_loser = max(scored[t]["score"] for t, _, _ in LOSERS)
    assert worst_winner > best_loser


def test_rsi_floor_direction_separates_winners_from_losers(scored):
    # The 5-for-5 separator from the design validation: rising RSI floors
    # preceded every win, falling RSI floors marked both losers.
    for ticker, _, _ in WINNERS:
        assert scored[ticker]["rsi_floor_delta"] > 0, ticker
    for ticker, _, _ in LOSERS:
        assert scored[ticker]["rsi_floor_delta"] < 0, ticker


def test_adbe_gets_fading_bounce_label(scored):
    assert scored["ADBE"]["setup"] == "Fading bounce"
    assert scored["ADBE"]["post_low_trend"] == "fading"


def test_amc_gets_knife_label(scored):
    assert scored["AMC"]["setup"] == "Knife"


def test_winners_get_constructive_labels(scored):
    for ticker, _, _ in WINNERS:
        assert scored[ticker]["setup"] in ("Exhaustion turn", "Basing glide"), ticker


def test_estc_capitulation_flush_not_penalized(scored):
    # ESTC's final leg undercut hard <10 bars after the prior swing low.
    # That flush must not open a new RSI floor (it would read as -8) and its
    # acceleration must not be scored bearish while RSI floors rise.
    estc = scored["ESTC"]
    assert estc["days_since_low"] <= 5
    assert estc["rsi_floor_delta"] > 0
    assert "Undercuts deepening" not in estc["signals"]


def test_days_since_low_exposed(scored):
    for ticker in scored:
        assert scored[ticker]["days_since_low"] is not None


def test_uptrend_pullback_track_unchanged(tmp_path):
    # Second track: a healthy pullback to a rising 50-MA still scores the
    # pre-existing +30 prime-entry points and gets its own label.
    scanner = StockScanner(api_key="test-key", cache_dir=str(tmp_path))
    result = {
        "ticker": "TEST",
        "price": 100.0,
        "high_52w": 110.0,  # -9% drawdown: recovery gate must NOT fire
        "trend": "Pullback in Uptrend",
        "dist_50": -1.0,
        "market_cap": 5_000_000_000,
        "days_since_low": 40,
    }
    score, signals, setup = scanner._score_opportunity(result)
    assert score == 30
    assert setup == "Uptrend pullback"
    assert "At 50-MA support (prime entry)" in signals


def test_recovery_metrics_require_60_bars():
    df = pd.DataFrame(
        {
            "close": [100.0] * 40,
            "low": [99.0] * 40,
            "high": [101.0] * 40,
            "volume": [1000] * 40,
        }
    )
    assert TechnicalIndicators.calculate_recovery_metrics(100.0, df) is None
