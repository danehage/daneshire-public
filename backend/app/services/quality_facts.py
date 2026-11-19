"""
Quality and value fact columns for the scanner.

Sortable facts, not a gate and not a score: nothing here feeds
``passes_quality_gate`` and no threshold lives here. They reuse the gate's
point-in-time statement filter and its TTM free cash flow and market cap, so
each is defined once.

Banks, insurers and REITs get no figures: free cash flow, gross margin and
invested capital don't describe those businesses. Neither does a row whose
sector is unknown, since it could be one of them. Missing data is ``None``,
never 0. Negative results stay negative.
"""

from datetime import date
from typing import Optional

from app.services.funnel import (
    FINANCIAL_SECTORS,
    FunnelConfig,
    QualityMetrics,
    _num,
    _sum,
    statements_as_of,
)

FACT_KEYS = (
    "fcf_yield_pct",
    "fcf_margin_pct",
    "roic_pct",
    "gross_margin_pct",
    "gross_margin_change_pts",
)


def _pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """numerator / denominator as a percent; None unless the base is positive."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100


def _gross_margin(quarters: list[dict]) -> Optional[float]:
    return _pct(_sum(quarters, "grossProfit"), _sum(quarters, "revenue"))


def _roic(q4: list[dict], balance: list[dict]) -> Optional[float]:
    """TTM operating income after tax / latest (debt + equity - cash).

    The tax rate is TTM tax / TTM pre-tax income. A tax credit counts as 0%;
    a pre-tax loss or a rate above 100% has no usable rate, so returns None.
    """
    if not balance:
        return None
    operating_income = _sum(q4, "operatingIncome")
    tax = _sum(q4, "incomeTaxExpense")
    pre_tax = _sum(q4, "incomeBeforeTax")
    if operating_income is None or tax is None or pre_tax is None or pre_tax <= 0:
        return None
    tax_rate = max(tax / pre_tax, 0.0)
    if tax_rate > 1:
        return None

    b = balance[0]
    debt = _num(b.get("totalDebt"))
    equity = _num(b.get("totalStockholdersEquity"))
    cash = _num(b.get("cashAndShortTermInvestments"))
    if cash is None:
        cash = _num(b.get("cashAndCashEquivalents"))
    if debt is None or equity is None or cash is None:
        return None
    return _pct(operating_income * (1 - tax_rate), debt + equity - cash)


def compute_quality_facts(
    income: list[dict],
    balance: list[dict],
    as_of: date,
    sector: Optional[str],
    metrics: Optional[QualityMetrics],
    cfg: FunnelConfig = FunnelConfig(),
) -> dict[str, Optional[float]]:
    """The fact columns as known on ``as_of``, each rounded to 2 places.

    ``metrics`` is the gate's ``compute_quality_metrics`` result for the same
    statements; it supplies TTM free cash flow and market cap.
    """
    facts: dict[str, Optional[float]] = dict.fromkeys(FACT_KEYS)
    if metrics is None or sector is None or sector in FINANCIAL_SECTORS:
        return facts

    lag = cfg.filing_lag_fallback_days
    inc = statements_as_of(income, as_of, lag)
    bal = statements_as_of(balance, as_of, lag)
    if len(inc) < 4:
        return facts
    q4 = inc[:4]

    gross_margin = _gross_margin(q4)
    prior_gross_margin = _gross_margin(inc[4:8]) if len(inc) >= 8 else None
    gross_margin_change = (
        gross_margin - prior_gross_margin
        if gross_margin is not None and prior_gross_margin is not None
        else None
    )

    values = {
        "fcf_yield_pct": _pct(metrics.fcf_ttm, metrics.market_cap),
        "fcf_margin_pct": _pct(metrics.fcf_ttm, _sum(q4, "revenue")),
        "roic_pct": _roic(q4, bal),
        "gross_margin_pct": gross_margin,
        "gross_margin_change_pts": gross_margin_change,
    }
    return {k: round(v, 2) if v is not None else None for k, v in values.items()}
