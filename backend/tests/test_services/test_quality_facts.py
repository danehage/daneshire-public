"""Quality and value fact columns. Pure: no database, no FMP."""

from datetime import date, timedelta

import pytest

from app.services.funnel import QualityMetrics, compute_quality_metrics
from app.services.quality_facts import FACT_KEYS, compute_quality_facts

AS_OF = date(2026, 11, 7)
ALL_BLANK = dict.fromkeys(FACT_KEYS)


def metrics(fcf=4e8, market_cap=1e10):
    return QualityMetrics(
        market_cap=market_cap, fcf_ttm=fcf, net_income_ttm=None, revenue_yoy_pct=None,
        revenue_down_2q=None, net_debt_ebitda=None, dilution_pct=None, altman_z=None,
        latest_filing=None,
    )


def income(n=8, **overrides):
    """``n`` quarters, newest first: $1B revenue, 60% gross margin (50% in the
    older four), $200M operating income, 25% tax."""
    rows = []
    for i in range(n):
        end = date(2026, 6, 30) - timedelta(days=91 * i)
        rows.append({
            "date": end.isoformat(),
            "filingDate": (end + timedelta(days=40)).isoformat(),
            "revenue": 1e9,
            "grossProfit": 6e8 if i < 4 else 5e8,
            "operatingIncome": 2e8,
            "incomeBeforeTax": 2e8,
            "incomeTaxExpense": 5e7,
            **overrides,
        })
    return rows


def balance(**overrides):
    return [{
        "date": "2026-06-30",
        "filingDate": "2026-08-09",
        "totalDebt": 2e9,
        "totalStockholdersEquity": 4e9,
        "cashAndShortTermInvestments": 1e9,
        "cashAndCashEquivalents": 5e8,
        **overrides,
    }]


def facts(inc=None, bal=None, sector="Industrials", m=None):
    return compute_quality_facts(
        income() if inc is None else inc,
        balance() if bal is None else bal,
        AS_OF,
        sector,
        metrics() if m is None else m,
    )


def test_baseline_figures():
    assert facts() == {
        "fcf_yield_pct": 4.0,  # 400M / 10B
        "fcf_margin_pct": 10.0,  # 400M / 4B
        "roic_pct": 12.0,  # 800M x 0.75 / (2B + 4B - 1B)
        "gross_margin_pct": 60.0,
        "gross_margin_change_pts": 10.0,
    }


def test_adobe_fiscal_q3_2026_as_reported():
    """FMP's figures for the quarters to 2026-08-28, at the $239.69 close."""
    quarters = [
        ("2026-08-28", "2026-09-22", 6760, 5957, 2354, 530, 2357),
        ("2026-05-29", "2026-06-15", 6618, 5903, 2238, 526, 2238),
        ("2026-02-27", "2026-03-25", 6398, 5734, 2418, 533, 2422),
        ("2025-11-28", "2026-01-15", 6194, 5545, 2261, 408, 2264),
        ("2025-08-29", "2025-09-24", 5988, 5346, 2173, 415, 2187),
        ("2025-05-30", "2025-06-25", 5873, 5235, 2109, 410, 2101),
        ("2025-02-28", "2025-03-26", 5714, 5092, 2163, 371, 2182),
        ("2024-11-29", "2025-01-13", 5606, 4990, 1957, 308, 1991),
    ]
    inc = [
        {"date": d, "filingDate": f, "revenue": r * 1e6, "grossProfit": g * 1e6,
         "operatingIncome": o * 1e6, "incomeTaxExpense": t * 1e6, "incomeBeforeTax": p * 1e6}
        for d, f, r, g, o, t, p in quarters
    ]
    bal = [{"date": "2026-08-28", "filingDate": "2026-09-22", "totalDebt": 6762e6,
            "totalStockholdersEquity": 11764e6, "cashAndShortTermInvestments": 5639e6}]
    cf = [
        {"date": row[0], "filingDate": row[1], "freeCashFlow": fcf * 1e6}
        for row, fcf in zip(quarters, (2438, 2107, 2921, 3126))
    ]
    m = compute_quality_metrics(inc, bal, cf, AS_OF, 239.69, market_cap=94_677_550_000)

    assert compute_quality_facts(inc, bal, AS_OF, "Technology", m) == {
        "fcf_yield_pct": 11.19,
        "fcf_margin_pct": 40.79,
        "roic_pct": 56.46,
        "gross_margin_pct": 89.1,
        "gross_margin_change_pts": -0.04,
    }


@pytest.mark.parametrize("sector", ["Financial Services", "Real Estate", None])
def test_no_figures_for_financials_reits_or_unknown_sectors(sector):
    assert facts(sector=sector) == ALL_BLANK


def test_utilities_keep_their_figures():
    assert facts(sector="Utilities")["fcf_yield_pct"] == 4.0


def test_no_metrics_or_too_few_quarters_is_blank():
    assert compute_quality_facts(income(), balance(), AS_OF, "Industrials", None) == ALL_BLANK
    assert facts(inc=income(3)) == ALL_BLANK


def test_negative_free_cash_flow_stays_negative():
    f = facts(m=metrics(fcf=-4e8))
    assert f["fcf_yield_pct"] == -4.0
    assert f["fcf_margin_pct"] == -10.0


def test_missing_free_cash_flow_or_market_cap_is_blank_not_zero():
    assert facts(m=metrics(fcf=None))["fcf_yield_pct"] is None
    assert facts(m=metrics(fcf=None))["fcf_margin_pct"] is None
    assert facts(m=metrics(market_cap=None))["fcf_yield_pct"] is None


@pytest.mark.parametrize("bad", [0, -1e9])
def test_zero_or_negative_denominators_are_blank(bad):
    assert facts(m=metrics(market_cap=bad))["fcf_yield_pct"] is None
    f = facts(inc=income(revenue=bad))
    assert f["fcf_margin_pct"] is None
    assert f["gross_margin_pct"] is None


def test_non_finite_inputs_are_blank():
    f = facts(inc=income(revenue=float("nan"), grossProfit=float("inf")))
    assert f["fcf_margin_pct"] is None
    assert f["gross_margin_pct"] is None


def test_gross_margin_change_needs_eight_quarters():
    f = facts(inc=income(7))
    assert f["gross_margin_pct"] == 60.0
    assert f["gross_margin_change_pts"] is None


def test_negative_gross_margin_stays_negative():
    assert facts(inc=income(grossProfit=-1e8))["gross_margin_pct"] == -10.0


def test_roic_falls_back_to_cash_when_short_term_investments_are_missing():
    bal = balance(cashAndShortTermInvestments=None)
    assert facts(bal=bal)["roic_pct"] == pytest.approx(600 / 55, abs=0.01)  # 600M / 5.5B


def test_roic_counts_a_tax_credit_as_zero_tax():
    assert facts(inc=income(incomeTaxExpense=-5e7))["roic_pct"] == 16.0  # 800M / 5B


def test_operating_loss_gives_negative_roic():
    assert facts(inc=income(operatingIncome=-2e8))["roic_pct"] == -12.0


@pytest.mark.parametrize(
    "inc_overrides,bal_overrides",
    [
        ({"incomeTaxExpense": None}, {}),
        ({"incomeBeforeTax": None}, {}),
        ({"incomeBeforeTax": 0}, {}),  # no usable tax rate
        ({"incomeBeforeTax": -2e8}, {}),
        ({"incomeTaxExpense": 3e8}, {}),  # rate above 100%
        ({"operatingIncome": None}, {}),
        ({}, {"totalDebt": None}),
        ({}, {"totalStockholdersEquity": None}),
        ({}, {"cashAndShortTermInvestments": None, "cashAndCashEquivalents": None}),
        ({}, {"totalStockholdersEquity": -1e9}),  # invested capital 0
        ({}, {"totalStockholdersEquity": -5e9}),  # invested capital negative
    ],
)
def test_roic_is_blank_without_a_usable_tax_rate_or_invested_capital(inc_overrides, bal_overrides):
    f = facts(inc=income(**inc_overrides), bal=balance(**bal_overrides))
    assert f["roic_pct"] is None
    assert f["fcf_yield_pct"] == 4.0  # the other columns still show


def test_roic_is_blank_without_any_balance_sheet():
    assert compute_quality_facts(income(), [], AS_OF, "Industrials", metrics())["roic_pct"] is None


def test_unfiled_quarters_are_not_used():
    """A quarter filed on or after as_of must not leak in."""
    inc = income(9)
    inc[0]["filingDate"] = AS_OF.isoformat()
    inc[0]["grossProfit"] = 9e8
    bal = balance() + [{**balance()[0], "date": "2026-03-31", "filingDate": "2026-05-09",
                        "totalStockholdersEquity": 9e9}]
    bal[0]["filingDate"] = AS_OF.isoformat()

    f = facts(inc=inc, bal=bal)
    assert f["gross_margin_pct"] == 57.5  # 3 quarters at 60%, 1 at 50%
    assert f["roic_pct"] == 6.0  # 600M / (2B + 9B - 1B), the older balance sheet
