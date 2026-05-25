"""
Scores a stock's fundamentals on a 0-100 scale using category-specific weights.
Five categories: quality_compounder, value_cyclical, turnaround,
                 speculative_growth, dividend_defensive
"""

from typing import Any

CATEGORIES = [
    "quality_compounder",
    "value_cyclical",
    "turnaround",
    "speculative_growth",
    "dividend_defensive",
]

# Weights per criterion per category. Values are multipliers (higher = more important).
# Each row sums to the same relative importance across the 10 criteria.
WEIGHTS = {
    #                           QC    VC    TA    SG    DD
    "pe_vs_sector":            [0.6,  1.0,  0.3,  0.2,  0.6],
    "peg":                     [1.0,  0.6,  0.2,  0.2,  0.2],
    "fcf_yield":               [1.0,  1.0,  0.6,  0.2,  1.0],
    "revenue_growth":          [0.6,  0.3,  0.6,  1.0,  0.3],
    "debt_equity":             [0.6,  1.0,  1.0,  0.2,  1.0],
    "roe_roic":                [1.0,  0.6,  0.3,  0.2,  0.6],
    "dividend":                [0.2,  0.2,  0.2,  0.2,  1.0],
    "insider_buys":            [0.6,  1.0,  1.0,  1.0,  0.3],
    "margin_trend":            [1.0,  1.0,  1.0,  0.6,  0.6],
    "inventory_trend":         [0.6,  1.0,  0.6,  0.2,  0.3],
}

CATEGORY_INDEX = {cat: i for i, cat in enumerate(CATEGORIES)}


def _score_metric(value: float | None, thresholds: list[tuple[float, int]]) -> int:
    """
    Maps a raw metric value to a score 0-5 using a list of (threshold, score) pairs
    sorted from best to worst. Returns 0 if value is None.
    """
    if value is None:
        return 0
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 1


def _score_pe(pe: float | None, sector_pe: float | None = 20.0) -> int:
    try:
        pe = float(pe)
    except (TypeError, ValueError):
        return 0
    if pe != pe:  # NaN check
        return 0
    if pe <= 0:  # negative earnings
        return 1
    ratio = pe / sector_pe if sector_pe else pe / 20.0
    if ratio < 0.7:
        return 5
    if ratio < 0.9:
        return 4
    if ratio < 1.1:
        return 3
    if ratio < 1.4:
        return 2
    return 1


def _score_peg(peg: float | None) -> int:
    return _score_metric(peg, [(0.01, 5), (0.01, 5)] if False else []) or _score_metric(
        peg,
        [(0.01, 5)],  # unreachable shortcut — use explicit logic below
    )


def _score_peg_value(peg: float | None) -> int:
    try:
        peg = float(peg)
    except (TypeError, ValueError):
        return 0
    if peg != peg:
        return 0
    if peg <= 0:
        return 1
    if peg < 0.8:
        return 5
    if peg < 1.2:
        return 4
    if peg < 1.8:
        return 3
    if peg < 2.5:
        return 2
    return 1


def _score_fcf_yield(fcf_yield: float | None) -> int:
    if fcf_yield is None:
        return 0
    return _score_metric(fcf_yield, [(8, 5), (5, 4), (3, 3), (1, 2), (0.01, 1)])


def _score_revenue_growth(growth: float | None) -> int:
    if growth is None:
        return 0
    pct = growth * 100
    if pct > 25:
        return 4  # cap high growth — could be unsustainable
    if pct > 15:
        return 5
    if pct > 8:
        return 4
    if pct > 3:
        return 3
    if pct > 0:
        return 2
    return 1  # declining revenue


def _score_debt_equity(de: float | None) -> int:
    if de is None:
        return 0
    if de < 0:  # more cash than debt
        return 5
    if de < 30:
        return 5
    if de < 80:
        return 4
    if de < 150:
        return 3
    if de < 300:
        return 2
    return 1


def _score_roe(roe: float | None) -> int:
    if roe is None:
        return 0
    pct = roe * 100
    return _score_metric(pct, [(20, 5), (15, 4), (10, 3), (5, 2), (0.01, 1)])


def _score_dividend(div_yield: float | None, payout_ratio: float | None) -> int:
    if div_yield is None or div_yield == 0:
        return 1  # no dividend
    pct = div_yield * 100
    if pct > 8:
        return 2  # suspiciously high — potential cut risk
    if pct > 4:
        score = 5
    elif pct > 2:
        score = 4
    else:
        score = 3
    # Penalise if payout ratio > 90% (unsustainable)
    if payout_ratio and payout_ratio > 0.9:
        score = max(1, score - 2)
    return score


def _score_insider_buys(insider_ownership: float | None) -> int:
    if insider_ownership is None:
        return 2  # neutral
    pct = insider_ownership * 100
    if pct > 15:
        return 5
    if pct > 5:
        return 4
    if pct > 1:
        return 3
    return 2


def _score_margin_trend(op_margin: float | None, net_margin: float | None) -> int:
    if op_margin is None and net_margin is None:
        return 0
    margin = op_margin or net_margin or 0
    pct = margin * 100
    return _score_metric(pct, [(20, 5), (12, 4), (6, 3), (1, 2), (0.01, 1)])


def _score_inventory(revenue_growth: float | None) -> int:
    # Without time-series data we approximate: if revenue is growing,
    # inventory management is likely fine. Returns neutral 3 as placeholder
    # (a future enhancement can compare inventory/sales over 2 quarters).
    if revenue_growth is None:
        return 2
    return 4 if revenue_growth > 0 else 2


def score_stock(fundamentals: dict, category: str, sector_pe: float = 20.0) -> dict:
    """
    Scores a stock given its fundamentals dict and category.
    Returns a dict with total score (0-100) and per-criterion breakdown.
    """
    if category not in CATEGORY_INDEX:
        category = "quality_compounder"
    idx = CATEGORY_INDEX[category]

    raw_scores = {
        "pe_vs_sector":    _score_pe(fundamentals.get("pe"), sector_pe),
        "peg":             _score_peg_value(fundamentals.get("peg")),
        "fcf_yield":       _score_fcf_yield(fundamentals.get("fcf_yield")),
        "revenue_growth":  _score_revenue_growth(fundamentals.get("revenue_growth_yoy")),
        "debt_equity":     _score_debt_equity(fundamentals.get("debt_equity")),
        "roe_roic":        _score_roe(fundamentals.get("roe")),
        "dividend":        _score_dividend(
                               fundamentals.get("dividend_yield"),
                               fundamentals.get("payout_ratio")
                           ),
        "insider_buys":    _score_insider_buys(fundamentals.get("insider_ownership")),
        "margin_trend":    _score_margin_trend(
                               fundamentals.get("op_margin"),
                               fundamentals.get("net_margin")
                           ),
        "inventory_trend": _score_inventory(fundamentals.get("revenue_growth_yoy")),
    }

    weighted_scores = {}
    total_weight = 0.0
    weighted_sum = 0.0

    for criterion, raw in raw_scores.items():
        weight = WEIGHTS[criterion][idx]
        weighted_scores[criterion] = {
            "raw": raw,
            "weight": weight,
            "weighted": raw * weight,
        }
        weighted_sum += raw * weight
        total_weight += weight * 5  # max possible contribution per criterion

    total_score = int(weighted_sum / total_weight * 100) if total_weight > 0 else 0

    verdict = "AVOID"
    if total_score >= 70:
        verdict = "BUY_CANDIDATE"
    elif total_score >= 50:
        verdict = "WATCHLIST"

    return {
        "symbol": fundamentals.get("symbol", ""),
        "category": category,
        "total_score": total_score,
        "verdict": verdict,
        "breakdown": weighted_scores,
    }


def classify_category(fundamentals: dict) -> str:
    """
    Heuristically classifies a stock into one of 5 categories
    based on sector, growth, margins, and dividend.
    Claude will refine this classification during AI analysis.
    """
    sector = (fundamentals.get("sector") or "").lower()
    div_yield = fundamentals.get("dividend_yield") or 0
    rev_growth = fundamentals.get("revenue_growth_yoy") or 0
    op_margin = fundamentals.get("op_margin") or 0
    net_margin = fundamentals.get("net_margin") or 0
    roe = fundamentals.get("roe") or 0
    pe = fundamentals.get("pe")

    # Dividend/defensive: utilities, consumer staples, telecoms with dividend
    if div_yield > 0.025 and sector in ("utilities", "consumer defensive", "communication services"):
        return "dividend_defensive"

    # Dividend/defensive: any stock with >4% yield
    if div_yield > 0.04:
        return "dividend_defensive"

    # Quality compounder: high margins, high ROE, moderate growth
    if op_margin > 0.18 and roe > 0.15 and rev_growth > 0.05:
        return "quality_compounder"

    # Speculative growth: high growth, low/no profit
    if rev_growth > 0.20 and (net_margin is None or net_margin < 0.05):
        return "speculative_growth"

    # Turnaround: low/negative margins or earnings, pe is high or negative
    if net_margin is not None and net_margin < 0.03:
        return "turnaround"
    if pe is not None and pe > 40:
        return "turnaround"

    # Value/cyclical: energy, materials, industrials, financials
    if sector in ("energy", "basic materials", "industrials", "financial services", "real estate"):
        return "value_cyclical"

    return "quality_compounder"  # default
