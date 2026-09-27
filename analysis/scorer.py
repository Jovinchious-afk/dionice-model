"""
Scores a stock's fundamentals on a 0-100 scale using category-specific weights.
Five categories: quality_compounder, value_cyclical, turnaround,
                 speculative_growth, dividend_defensive

Version 2 (2026-09):
  - P/E is compared with the median of the stock's industry (or sector) from
    data/sector_benchmarks.json instead of a fixed 20 — the investor's own brief
    asks for valuation "vs the industry"
  - insider_buys scores actual open-market purchases in ~2 years, not the % of
    shares insiders own
  - margin_trend adjusts the margin level for the trend (3 falling quarters = -1)
  - a metric Yahoo does not report is left out of the average instead of scoring
    0, which used to rank a missing PEG below a terrible one
"""

import json
from functools import lru_cache
from pathlib import Path

from analysis.sector_context import is_cyclical

SCORER_VERSION = 2

CATEGORIES = [
    "quality_compounder",
    "value_cyclical",
    "turnaround",
    "speculative_growth",
    "dividend_defensive",
]

# Weights per criterion per category. Values are multipliers (higher = more important).
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

DEFAULT_PE = 20.0
BENCHMARKS_PATH = Path(__file__).parent.parent / "data" / "sector_benchmarks.json"
MIN_INDUSTRY_PEERS = 8
# Below this share of the category's total weight the score says little
MIN_COVERAGE = 0.5

# Balance-sheet distress tests (Altman Z, current ratio) were derived from
# manufacturing firms and misfire badly outside it: regulated utilities and REITs
# run high leverage against stable or asset-backed cash flows, and banks and
# insurers have no working-capital cycle at all. Measured examples of blue chips
# these checks wrongly excluded: AWK Z=1.01, PPL Z=0.98, CubeSmart CR=0.10,
# Equity Residential CR=0.13, Progressive CR=0.29.
BALANCE_SHEET_EXEMPT_SECTORS = {
    "utilities",
    "financial services",
    "financials",
    "real estate",
}


@lru_cache(maxsize=1)
def _benchmarks() -> dict:
    try:
        return json.loads(BENCHMARKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def peer_pe(fundamentals: dict, forward: bool = False) -> tuple[float, str]:
    """Median (forward) P/E of the stock's industry, else its sector, else 20."""
    key = "forward_pe_median" if forward else "pe_median"
    data = _benchmarks()
    industry = (data.get("industries") or {}).get(fundamentals.get("industry") or "")
    if industry and (industry.get("n") or 0) >= MIN_INDUSTRY_PEERS and industry.get(key):
        return float(industry[key]), f"industrija {fundamentals.get('industry')}"
    sector = (data.get("sectors") or {}).get(fundamentals.get("sector") or "")
    if sector and sector.get(key):
        return float(sector[key]), f"sektor {fundamentals.get('sector')}"
    return DEFAULT_PE, "zadano 20"


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _score_metric(value: float | None, thresholds: list[tuple[float, int]]) -> int | None:
    """
    Maps a raw metric value to a score 1-5 using (threshold, score) pairs sorted
    from best to worst. None when the metric is missing.
    """
    if value is None:
        return None
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 1


def _score_pe(pe, peer: float = DEFAULT_PE) -> int | None:
    pe = _num(pe)
    if pe is None:
        return None
    if pe <= 0:  # negative earnings
        return 1
    ratio = pe / peer if peer else pe / DEFAULT_PE
    if ratio < 0.7:
        return 5
    if ratio < 0.9:
        return 4
    if ratio < 1.1:
        return 3
    if ratio < 1.4:
        return 2
    return 1


def _score_peg_value(peg) -> int | None:
    peg = _num(peg)
    if peg is None:
        return None
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


def _score_fcf_yield(fcf_yield) -> int | None:
    return _score_metric(_num(fcf_yield), [(8, 5), (5, 4), (3, 3), (1, 2), (0.01, 1)])


def _score_revenue_growth(growth) -> int | None:
    growth = _num(growth)
    if growth is None:
        return None
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


def _score_debt_equity(de) -> int | None:
    """de is yfinance's percent figure (49.0 = 0.49x)."""
    de = _num(de)
    # Negative D/E means negative equity (buybacks beyond book value, e.g. MCD):
    # the ratio says nothing then, so it is left out rather than scored as 5
    if de is None or de < 0:
        return None
    if de < 30:
        return 5
    if de < 80:
        return 4
    if de < 150:
        return 3
    if de < 300:
        return 2
    return 1


def _score_roe(roe) -> int | None:
    roe = _num(roe)
    if roe is None:
        return None
    return _score_metric(roe * 100, [(20, 5), (15, 4), (10, 3), (5, 2), (0.01, 1)])


def _score_dividend(div_yield, payout_ratio) -> int:
    """No dividend is a fact (1), not missing data."""
    div_yield = _num(div_yield)
    if not div_yield:
        return 1
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
    payout = _num(payout_ratio)
    if payout and payout > 0.9:
        score = max(1, score - 2)
    return score


def _score_insider_buys(buys_24m, unclassified_24m, insider_ownership) -> int:
    """
    The investor's rule: insider selling is not a signal, but no executive purchase
    in two years is a warning. Neutral (2) when Yahoo has no list at all.
    """
    buys = _num(buys_24m)
    if buys is not None:
        if buys >= 2:
            return 5
        if buys >= 1:
            return 4
        return 3 if (_num(unclassified_24m) or 0) > 0 else 2
    ownership = _num(insider_ownership)
    if ownership is not None and ownership > 0.15:
        return 3  # owners with a big stake, but no purchase data
    return 2


def _score_margin_trend(op_margin, net_margin, declining_3q=None, quarters=None) -> int | None:
    """Margin level, one point lower after three falling quarters, one higher when clearly rising."""
    margin = _num(op_margin)
    if margin is None:
        margin = _num(net_margin)
    if margin is None:
        return None
    score = _score_metric(margin * 100, [(20, 5), (12, 4), (6, 3), (1, 2), (0.01, 1)])
    if declining_3q:
        score = max(1, score - 1)
    elif quarters and len(quarters) >= 4 and quarters[0] - quarters[3] >= 2.0:
        score = min(5, score + 1)
    return score


def _score_inventory(inventory_growth, sales_growth) -> int:
    """
    Inventory vs sales, same quarter a year apart. The investor's rule: inventories
    growing twice as fast as sales = sell. No inventory line (software, banks) or
    no data is neutral rather than a penalty.
    """
    inventory_growth, sales_growth = _num(inventory_growth), _num(sales_growth)
    if inventory_growth is None or sales_growth is None:
        return 3
    if inventory_growth > 0.10 and inventory_growth > 2 * max(sales_growth, 0.0):
        return 1
    if inventory_growth <= sales_growth:
        return 5
    if inventory_growth <= sales_growth + 0.05:
        return 4
    if inventory_growth <= sales_growth + 0.10:
        return 3
    return 2


def resolve_weights(override: dict | None) -> dict:
    """WEIGHTS with an approved override (from model_params) applied; malformed rows ignored."""
    if not override:
        return WEIGHTS
    merged = {k: list(v) for k, v in WEIGHTS.items()}
    for criterion, row in override.items():
        if criterion in merged and isinstance(row, list) and len(row) == len(CATEGORIES):
            try:
                merged[criterion] = [max(0.0, float(w)) for w in row]
            except (TypeError, ValueError):
                continue
    return merged


def score_stock(fundamentals: dict, category: str, weights: dict | None = None) -> dict:
    """
    Scores a stock given its fundamentals dict and category.
    Returns total score (0-100), per-criterion breakdown and data coverage.
    """
    if category not in CATEGORY_INDEX:
        category = "quality_compounder"
    idx = CATEGORY_INDEX[category]
    weights = weights or WEIGHTS

    # A cyclical's trailing P/E is inflated by depressed earnings at the bottom of
    # the cycle (GNRC: 42.9 trailing vs 15.8 forward), so judge it on forward P/E.
    use_forward = category == "value_cyclical" and _num(fundamentals.get("forward_pe")) is not None
    pe = fundamentals.get("forward_pe") if use_forward else fundamentals.get("pe")
    peer, peer_source = peer_pe(fundamentals, forward=use_forward)
    quarterly_sales = fundamentals.get("quarterly_revenue_growth_yoy")
    sales_growth = quarterly_sales if quarterly_sales is not None else fundamentals.get("revenue_growth_yoy")

    raw_scores = {
        "pe_vs_sector":    _score_pe(pe, peer),
        "peg":             _score_peg_value(fundamentals.get("peg")),
        "fcf_yield":       _score_fcf_yield(fundamentals.get("fcf_yield")),
        "revenue_growth":  _score_revenue_growth(fundamentals.get("revenue_growth_yoy")),
        "debt_equity":     _score_debt_equity(fundamentals.get("debt_equity")),
        "roe_roic":        _score_roe(fundamentals.get("roe")),
        "dividend":        _score_dividend(fundamentals.get("dividend_yield"), fundamentals.get("payout_ratio")),
        "insider_buys":    _score_insider_buys(
                               fundamentals.get("insider_buys_24m"),
                               fundamentals.get("insider_unclassified_24m"),
                               fundamentals.get("insider_ownership"),
                           ),
        "margin_trend":    _score_margin_trend(
                               fundamentals.get("op_margin"),
                               fundamentals.get("net_margin"),
                               fundamentals.get("op_margin_declining_3q"),
                               fundamentals.get("op_margin_quarters_pct"),
                           ),
        "inventory_trend": _score_inventory(fundamentals.get("inventory_growth_yoy"), sales_growth),
    }

    breakdown = {}
    weighted_sum = available_weight = full_weight = 0.0
    for criterion, raw in raw_scores.items():
        weight = weights[criterion][idx]
        full_weight += weight
        breakdown[criterion] = {"raw": raw, "weight": weight, "weighted": raw * weight if raw is not None else None}
        if raw is not None:
            weighted_sum += raw * weight
            available_weight += weight

    coverage = available_weight / full_weight if full_weight else 0.0
    total_score = int(weighted_sum / (available_weight * 5) * 100) if available_weight else 0

    if coverage < MIN_COVERAGE:
        verdict = "INSUFFICIENT_DATA"
    elif total_score >= 70:
        verdict = "BUY_CANDIDATE"
    elif total_score >= 50:
        verdict = "WATCHLIST"
    else:
        verdict = "AVOID"

    return {
        "symbol": fundamentals.get("symbol", ""),
        "category": category,
        "total_score": total_score,
        "coverage": round(coverage, 2),
        "verdict": verdict,
        "breakdown": breakdown,
        "peer_pe": round(peer, 1),
        "peer_pe_source": peer_pe_label(peer_source, use_forward),
        "scorer_version": SCORER_VERSION,
    }


def peer_pe_label(source: str, forward: bool) -> str:
    return f"{'forward ' if forward else ''}P/E medijan ({source})"


def hard_exclude(fund: dict, category: str) -> tuple[bool, str]:
    """
    Returns (should_exclude, reason). True = drop this ticker before AI analysis.
    Filters out structurally broken companies regardless of score.
    Portfolio positions bypass this check in run_weekly.py.
    """
    market_cap = fund.get("market_cap") or 0
    if market_cap and market_cap < 30_000_000:
        return True, f"market_cap ${market_cap/1e6:.1f}M < $30M"

    sector = (fund.get("sector") or "").strip().lower()
    ratio_meaningful = sector not in BALANCE_SHEET_EXEMPT_SECTORS

    cr = fund.get("current_ratio")
    if ratio_meaningful and cr is not None and cr < 0.3:
        return True, f"current_ratio {cr:.2f} < 0.3 (likely insolvent)"

    de = fund.get("debt_equity")
    cr_val = cr or 1.0
    if ratio_meaningful and de is not None and de > 600 and cr_val < 0.5:
        return True, f"D/E {de:.0f} + CR {cr_val:.2f} — extreme leverage + illiquidity"

    if category not in ("speculative_growth",):
        fcf_yield = fund.get("fcf_yield")
        if fcf_yield is not None and fcf_yield < -40.0:
            return True, f"FCF yield {fcf_yield:.1f}% < -40% (cash burning too fast)"

        # Altman Z-Score < 1.81 = bankruptcy distress zone. Speculative_growth stocks
        # are pre-profit by design and routinely score low here, so this check is
        # skipped for that category — flagged as context in the AI prompt instead.
        # Also skipped for the sectors it was never built for — see
        # BALANCE_SHEET_EXEMPT_SECTORS.
        z = fund.get("altman_z_score")
        if z is not None and z < 1.81 and sector not in BALANCE_SHEET_EXEMPT_SECTORS:
            return True, f"Altman Z-Score {z:.2f} < 1.81 (bankruptcy distress zone)"

    # Liquidity: lower threshold for speculative/small-cap (gems), stricter for main universe
    avg_vol = fund.get("avg_volume")
    vol_threshold = 10_000 if category == "speculative_growth" else 50_000
    if avg_vol is not None and avg_vol < vol_threshold:
        return True, f"avg_volume {avg_vol:,.0f}/day < {vol_threshold:,} (illiquid)"

    # Analyst consensus: only exclude strong_sell/underperform, not plain "sell"
    rec = (fund.get("analyst_recommendation") or "").lower()
    if rec in ("strong_sell", "underperform"):
        return True, f"analyst consensus: {rec}"

    return False, ""


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

    # Cyclicals before everything else: the investor's rules treat them differently
    # (inventories, cycle position, inverted P/E) and warn against buying them for
    # income. Checked before the P/E > 40 turnaround rule, which misfiled GNRC —
    # a cyclical at the bottom of its cycle — as a turnaround.
    if is_cyclical(fundamentals):
        return "value_cyclical"

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
