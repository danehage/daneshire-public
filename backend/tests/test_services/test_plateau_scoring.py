"""
Fixture tests for the sell-side plateau grade (issue #57).

Fixtures are REAL FMP daily bars frozen on 2026-07-08 (the #57 design/validation
session), truncated at each trade's sell date — the five real exits plus the two
underwater live holdings. Real fingerprints at the sell date:

  NFLX sold 4/14 @ $106.27 → Climax          (RSI 77.3 at run high, +17.4% over 50-MA)
  ESTC sold 5/29 @ $62.34  → Climax          (RSI 75.9 at run high, +28.7% over 50-MA)
  APD  sold 7/1  @ $305.62 → Thesis complete (89.8% of pre-entry 52w high)
  EOG  sold 6/17 @ $132.94 → Thesis complete (96.4% gap; RSI delta −27.5 fade)
  AJG  sold 6/26 @ $224.88 → Running         (RSI ceilings flat +0.1, +7.4% ext)
  RBLX (open)  as of 7/8   → Broken          (underwater vs $73.755 cost basis)
  TMUS (open)  as of 7/8   → Broken          (underwater vs $191.44 cost basis)

Acceptance (from the #57 issue): NFLX/ESTC = Climax, APD/EOG sell-ish,
AJG = Running with a sell-pressure score materially below the other four,
and underwater holdings read Broken — never Running.

pre_entry_high_52w is computed from the fixture bars before the open date,
mirroring exactly what the run-plateau-checks endpoint does in production.
"""

import json
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from app.services.indicators import TechnicalIndicators

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "plateau"

# (ticker, as_of, open_date, entry_price, expected_label)
# Entry prices: exit fills from the #57 validation table; live cost bases from
# the DaneBrain vault frontmatter.
CASES = [
    ("NFLX", "2026-04-14", "2025-12-29", 93.35, "Climax"),
    ("ESTC", "2026-05-29", "2026-04-15", 47.605, "Climax"),
    ("APD", "2026-07-01", "2026-01-02", 248.72, "Thesis complete"),
    ("EOG", "2026-06-17", "2026-01-02", 105.795, "Thesis complete"),
    ("AJG", "2026-06-26", "2026-05-21", 207.81, "Running"),
    ("RBLX", "2026-07-08", "2026-04-15", 73.755, "Broken"),
    ("TMUS", "2026-07-08", "2026-04-15", 191.44, "Broken"),
]


def _grade_fixture(
    ticker: str,
    as_of: str,
    open_date: Optional[str],
    entry_price: Optional[float],
) -> dict:
    """Load a frozen fixture and run the plateau grade path.

    Mirrors run-plateau-checks: pre-entry 52w high from the 252 bars ending at
    the open date, grading window = last 252 bars up to the as-of date.
    """
    with open(FIXTURES / f"{ticker}.json") as f:
        rows = json.load(f)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] <= pd.Timestamp(as_of)].sort_values("date").reset_index(drop=True)

    pre_entry_high = None
    if open_date is not None:
        pre = df[df["date"] <= pd.Timestamp(open_date)].tail(252)
        if len(pre) >= 60:
            pre_entry_high = float(pre["high"].max())

    df = df.tail(252).reset_index(drop=True)
    current_price = float(df["close"].iloc[-1])
    metrics = TechnicalIndicators.calculate_plateau_metrics(
        current_price=current_price,
        df=df,
        open_date=open_date,
        pre_entry_high_52w=pre_entry_high,
        entry_price=entry_price,
    )
    assert metrics is not None, f"{ticker}: calculate_plateau_metrics returned None"
    metrics["price"] = current_price
    label, score, signals, stop = TechnicalIndicators.grade_holding(metrics)
    return {
        "label": label,
        "score": score,
        "signals": signals,
        "stop": stop,
        **metrics,
    }


@pytest.fixture(scope="module")
def graded():
    return {
        ticker: _grade_fixture(ticker, as_of, open_date, entry_price)
        for ticker, as_of, open_date, entry_price, _ in CASES
    }


def test_all_expected_labels(graded):
    for ticker, _, _, _, expected in CASES:
        assert graded[ticker]["label"] == expected, (
            f"{ticker}: label={graded[ticker]['label']!r}, expected {expected!r}"
        )


def test_sell_pressure_separates_winners_from_ajg(graded):
    ajg_score = graded["AJG"]["score"]
    for ticker in ("NFLX", "ESTC", "APD", "EOG"):
        assert graded[ticker]["score"] > ajg_score, (
            f"{ticker} scored {graded[ticker]['score']}, AJG scored {ajg_score}"
        )


def test_climax_conditions_met(graded):
    """Both RSI and extension must be at Climax thresholds for NFLX and ESTC."""
    for ticker in ("NFLX", "ESTC"):
        g = graded[ticker]
        assert g["rsi_at_newest_ceiling"] is not None and g["rsi_at_newest_ceiling"] >= 75.0, (
            f"{ticker}: rsi_at_newest_ceiling={g['rsi_at_newest_ceiling']}"
        )
        assert g["ext_vs_50ma"] is not None and g["ext_vs_50ma"] >= 15.0, (
            f"{ticker}: ext_vs_50ma={g['ext_vs_50ma']}"
        )


def test_thesis_gap_conditions_met(graded):
    """gap_closed_pct must be >= 88% for the Thesis complete cases."""
    for ticker in ("APD", "EOG"):
        g = graded[ticker]
        assert g["gap_closed_pct"] is not None and g["gap_closed_pct"] >= 88.0, (
            f"{ticker}: gap_closed_pct={g['gap_closed_pct']}"
        )


def test_ajg_ext_below_climax_threshold(graded):
    """AJG is in early innings — extension is well below the Climax threshold."""
    assert graded["AJG"]["ext_vs_50ma"] is not None
    assert graded["AJG"]["ext_vs_50ma"] < 15.0, (
        f"AJG ext_vs_50ma={graded['AJG']['ext_vs_50ma']:.1f}% unexpectedly >= 15%"
    )


def test_rsi_delta_catches_eog_fade(graded):
    """The RSI delta compares the newest SWING HIGH against the prior ceiling.

    EOG's fade (RSI 85.5 ceiling, then lower swing highs at ~59/58) never sets
    new ceilings, so a ceilings-only delta reads it as strengthening (+24)
    instead of exhausting (−27.5). Pins the definition from the validated
    reference (plateau_validation.py, 2026-07-08 session).
    """
    assert graded["EOG"]["rsi_ceiling_delta"] is not None
    assert graded["EOG"]["rsi_ceiling_delta"] < -20.0, (
        f"EOG rsi_ceiling_delta={graded['EOG']['rsi_ceiling_delta']} — "
        "expected a deep negative fade; is the delta ceilings-only again?"
    )


def test_underwater_holdings_read_broken_not_running(graded):
    """Design call: every holding gets a label — an underwater position must
    surface as Broken (full information), never as a healthy 'Running'."""
    for ticker in ("RBLX", "TMUS"):
        g = graded[ticker]
        assert g["label"] == "Broken", f"{ticker}: {g['label']!r}"
        assert g["price"] < CASES[[c[0] for c in CASES].index(ticker)][3]


def test_days_since_high_exposed(graded):
    for ticker in graded:
        assert graded[ticker]["days_since_high"] is not None


def test_stop_suggestion_field_exists(graded):
    for ticker in graded:
        assert "stop" in graded[ticker]


def test_plateau_metrics_require_60_bars():
    df = pd.DataFrame(
        {
            "close": [100.0] * 40,
            "low": [99.0] * 40,
            "high": [101.0] * 40,
            "volume": [1000] * 40,
        }
    )
    assert TechnicalIndicators.calculate_plateau_metrics(100.0, df) is None


def test_label_precedence_broken_wins_over_thesis():
    """Broken wins even when gap >= 88%."""
    result = {
        "price": 90.0,
        "rsi_ceiling_delta": 5.0,
        "rsi_at_newest_ceiling": 50.0,
        "overshoot_decelerating": False,
        "post_high_trend": None,
        "ext_vs_50ma": 5.0,
        "gap_closed_pct": 95.0,
        "down_up_vol_ratio": 1.0,
        "minor_swing_floor": 95.0,  # price (90) < floor (95) → Broken
        "atr14": 2.0,
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert label == "Broken"


def test_broken_from_entry_base_alone():
    """Underwater vs entry triggers Broken even with no swing floor below price."""
    result = {
        "price": 90.0,
        "rsi_ceiling_delta": None,
        "rsi_at_newest_ceiling": 50.0,
        "overshoot_decelerating": None,
        "post_high_trend": None,
        "ext_vs_50ma": 5.0,
        "gap_closed_pct": None,
        "down_up_vol_ratio": 1.0,
        "minor_swing_floor": 85.0,  # price above the floor
        "atr14": 2.0,
        "entry_price": 100.0,  # but underwater vs entry → Broken
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert label == "Broken"
    assert any("entry base" in s for s in signals)


def test_label_precedence_climax_over_thesis():
    """Climax wins over Thesis complete when both conditions are met."""
    result = {
        "price": 110.0,
        "rsi_ceiling_delta": -1.0,
        "rsi_at_newest_ceiling": 78.0,
        "overshoot_decelerating": False,
        "post_high_trend": None,
        "ext_vs_50ma": 18.0,
        "gap_closed_pct": 90.0,
        "down_up_vol_ratio": 1.0,
        "minor_swing_floor": 100.0,
        "atr14": 2.0,
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert label == "Climax"
    # Distinct ping copy: the parabolic signal must lead the message.
    assert signals and signals[0].startswith("Parabolic")


def test_label_precedence_thesis_over_cresting():
    """Thesis complete wins over Cresting when gap >= 88%."""
    result = {
        "price": 110.0,
        "rsi_ceiling_delta": -5.0,  # would trigger Cresting
        "rsi_at_newest_ceiling": 60.0,
        "overshoot_decelerating": True,  # would trigger Cresting
        "post_high_trend": None,
        "ext_vs_50ma": 5.0,
        "gap_closed_pct": 90.0,  # ≥ 88% → Thesis complete
        "down_up_vol_ratio": 1.0,
        "minor_swing_floor": 100.0,
        "atr14": 2.0,
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert label == "Thesis complete"


def test_distribution_label():
    """Distribution label requires fading post-high AND distribution volume."""
    result = {
        "price": 110.0,
        "rsi_ceiling_delta": 0.0,
        "rsi_at_newest_ceiling": 60.0,
        "overshoot_decelerating": False,
        "post_high_trend": "fading",
        "ext_vs_50ma": 5.0,
        "gap_closed_pct": None,
        "down_up_vol_ratio": 1.2,  # > 1.05 → Distribution vol
        "minor_swing_floor": 100.0,
        "atr14": 2.0,
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert label == "Distribution"


def test_distribution_volume_adds_sell_pressure():
    """Down/up volume > 1.05 is a sell-side confirmation — it must raise the
    score, not lower it (the sign bug the first pass shipped)."""
    base = {
        "price": 110.0,
        "rsi_ceiling_delta": -5.0,
        "rsi_at_newest_ceiling": 60.0,
        "overshoot_decelerating": False,
        "post_high_trend": None,
        "ext_vs_50ma": 5.0,
        "gap_closed_pct": None,
        "minor_swing_floor": 100.0,
        "atr14": 2.0,
    }
    _, quiet_score, _, _ = TechnicalIndicators.grade_holding(
        {**base, "down_up_vol_ratio": 1.0}
    )
    _, distribution_score, _, _ = TechnicalIndicators.grade_holding(
        {**base, "down_up_vol_ratio": 1.2}
    )
    assert distribution_score > quiet_score


def test_stop_suggestion_includes_trail():
    """Trail stop is always included when ATR14 is available."""
    result = {
        "price": 100.0,
        "rsi_ceiling_delta": None,
        "rsi_at_newest_ceiling": None,
        "overshoot_decelerating": None,
        "post_high_trend": None,
        "ext_vs_50ma": None,
        "gap_closed_pct": None,
        "down_up_vol_ratio": 1.0,
        "minor_swing_floor": None,
        "atr14": 4.0,
    }
    label, score, signals, stop = TechnicalIndicators.grade_holding(result)
    assert stop is not None and "Trail" in stop
    assert "ATR" in stop
