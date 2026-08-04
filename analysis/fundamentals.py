"""
Fetches fundamental data for a list of stock tickers using yfinance.
Results are cached locally for 24 hours to avoid Yahoo Finance rate limits.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yfinance as yf

CACHE_PATH = Path(__file__).parent.parent / "data" / "yfinance_cache.json"
CACHE_TTL_HOURS = 24


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")


def _is_fresh(entry: dict) -> bool:
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        return datetime.now(timezone.utc) - fetched_at < timedelta(hours=CACHE_TTL_HOURS)
    except (KeyError, ValueError):
        return False


def _safe_get(info: dict, key: str, default=None) -> Any:
    val = info.get(key)
    if val is None or val != val:  # NaN check
        return default
    return val


def _compute_altman_z(stock, market_cap: float | None) -> float | None:
    """
    Altman Z-Score (bankruptcy risk): Z > 2.99 safe, 1.81-2.99 grey, < 1.81 distress.
    Needs balance sheet + income statement — returns None if any input is missing.
    """
    if not market_cap:
        return None
    try:
        bs = stock.balance_sheet
        inc = stock.financials
        if bs.empty or inc.empty:
            return None
        col_bs, col_inc = bs.columns[0], inc.columns[0]

        def _get(df, col, label):
            if label not in df.index:
                return None
            val = df.loc[label, col]
            return None if val != val else float(val)  # NaN check

        wc = _get(bs, col_bs, "Working Capital")
        ta = _get(bs, col_bs, "Total Assets")
        re = _get(bs, col_bs, "Retained Earnings")
        tl = _get(bs, col_bs, "Total Liabilities Net Minority Interest")
        rev = _get(inc, col_inc, "Total Revenue")
        ebit = _get(inc, col_inc, "EBIT")

        if None in (wc, ta, re, tl, rev, ebit) or not ta or not tl:
            return None

        z = 1.2 * (wc / ta) + 1.4 * (re / ta) + 3.3 * (ebit / ta) + 0.6 * (market_cap / tl) + 1.0 * (rev / ta)
        return round(z, 2)
    except Exception:
        return None


def _compute_relative_strength(stock, spy_return_6m: float | None) -> float | None:
    """Own 6-month return minus S&P 500's 6-month return, in percentage points."""
    if spy_return_6m is None:
        return None
    try:
        closes = stock.history(period="6mo", auto_adjust=True)["Close"].dropna()
        if len(closes) < 2:
            return None
        start, end = float(closes.iloc[0]), float(closes.iloc[-1])
        if start <= 0:
            return None
        own_return = (end - start) / start * 100
        return round(own_return - spy_return_6m, 1)
    except Exception:
        return None


def _fetch_news_headlines(stock, limit: int = 3) -> list[str]:
    try:
        news = stock.news or []
        headlines = []
        for item in news[:limit]:
            content = item.get("content", item)
            title = content.get("title")
            if title:
                headlines.append(title)
        return headlines
    except Exception:
        return []


def get_spy_return_6m() -> float | None:
    """Fetches the S&P 500's 6-month return once per batch, for relative-strength comparisons."""
    try:
        closes = yf.Ticker("SPY").history(period="6mo", auto_adjust=True)["Close"].dropna()
        if len(closes) < 2:
            return None
        start, end = float(closes.iloc[0]), float(closes.iloc[-1])
        if start <= 0:
            return None
        return round((end - start) / start * 100, 1)
    except Exception:
        return None


def fetch_fundamentals(ticker: str, spy_return_6m: float | None = None) -> dict:
    """
    Returns fundamental metrics for a ticker.
    Uses cache if data is <24h old; fetches from yfinance otherwise.
    Returns a dict with all metrics, or a minimal error dict if fetch fails.
    """
    cache = _load_cache()
    symbol = ticker.upper()

    if symbol in cache and _is_fresh(cache[symbol]):
        return cache[symbol]["data"]

    try:
        stock = yf.Ticker(symbol)
        info = stock.info or {}
        fast_info = stock.fast_info

        # --- Valuation ---
        pe = _safe_get(info, "trailingPE")
        forward_pe = _safe_get(info, "forwardPE")
        peg = _safe_get(info, "pegRatio")
        ps = _safe_get(info, "priceToSalesTrailing12Months")
        pb = _safe_get(info, "priceToBook")
        ev_ebitda = _safe_get(info, "enterpriseToEbitda")
        ev_sales = _safe_get(info, "enterpriseToRevenue")

        # --- Profitability ---
        gross_margin = _safe_get(info, "grossMargins")
        op_margin = _safe_get(info, "operatingMargins")
        net_margin = _safe_get(info, "profitMargins")
        roe = _safe_get(info, "returnOnEquity")
        roa = _safe_get(info, "returnOnAssets")

        # --- Balance sheet ---
        debt_equity = _safe_get(info, "debtToEquity")
        current_ratio = _safe_get(info, "currentRatio")
        quick_ratio = _safe_get(info, "quickRatio")
        cash_per_share = _safe_get(info, "totalCashPerShare")
        total_debt = _safe_get(info, "totalDebt")
        total_cash = _safe_get(info, "totalCash")

        # --- Cash flow ---
        fcf = _safe_get(info, "freeCashflow")
        market_cap = _safe_get(info, "marketCap")
        fcf_yield = (fcf / market_cap * 100) if fcf and market_cap else None

        # --- Growth ---
        revenue_growth = _safe_get(info, "revenueGrowth")  # YoY
        earnings_growth = _safe_get(info, "earningsGrowth")  # YoY
        earnings_quarterly_growth = _safe_get(info, "earningsQuarterlyGrowth")

        # --- Capital returns ---
        shares_outstanding = _safe_get(info, "sharesOutstanding")
        shares_float = _safe_get(info, "floatShares")
        dividend_yield = _safe_get(info, "dividendYield")
        payout_ratio = _safe_get(info, "payoutRatio")

        # --- Insider / institutional ---
        insider_ownership = _safe_get(info, "heldPercentInsiders")
        institutional_ownership = _safe_get(info, "heldPercentInstitutions")
        short_ratio = _safe_get(info, "shortRatio")
        short_percent = _safe_get(info, "shortPercentOfFloat")

        # --- Market data ---
        current_price = _safe_get(info, "currentPrice") or _safe_get(info, "regularMarketPrice")
        week_52_high = _safe_get(info, "fiftyTwoWeekHigh")
        week_52_low = _safe_get(info, "fiftyTwoWeekLow")
        avg_volume = _safe_get(info, "averageVolume")
        avg_volume_10d = _safe_get(info, "averageVolume10days")

        # 52-week position (0% = at low, 100% = at high)
        week_52_position = None
        if week_52_high and week_52_low and current_price and week_52_high != week_52_low:
            week_52_position = (current_price - week_52_low) / (week_52_high - week_52_low) * 100

        # --- Classification metadata ---
        sector = _safe_get(info, "sector", "Unknown")
        industry = _safe_get(info, "industry", "Unknown")
        country = _safe_get(info, "country", "Unknown")
        business_summary = _safe_get(info, "longBusinessSummary", "")[:500]
        analyst_target = _safe_get(info, "targetMeanPrice")
        recommendation = _safe_get(info, "recommendationKey", "none")

        # --- Earnings calendar ---
        next_earnings_date = None
        next_earnings_days = None
        try:
            cal = stock.calendar
            if cal is not None:
                ed = None
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if isinstance(ed, list) and ed:
                        ed = ed[0]
                elif hasattr(cal, "columns") and len(cal.columns) > 0:
                    ed = cal.columns[0]
                if ed is not None:
                    from datetime import date as _date
                    if hasattr(ed, "date"):
                        earnings_dt = ed.date()
                    else:
                        earnings_dt = datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
                    next_earnings_date = earnings_dt.strftime("%Y-%m-%d")
                    next_earnings_days = (earnings_dt - _date.today()).days
        except Exception:
            pass

        # --- New signals: bankruptcy risk, momentum, news ---
        altman_z_score = _compute_altman_z(stock, market_cap)
        relative_strength_6m = _compute_relative_strength(stock, spy_return_6m)
        news_headlines = _fetch_news_headlines(stock)

        data = {
            "symbol": symbol,
            "name": _safe_get(info, "longName", symbol),
            "sector": sector,
            "industry": industry,
            "country": country,
            "business_summary": business_summary,
            "current_price": current_price,
            "market_cap": market_cap,
            # Valuation
            "pe": pe,
            "forward_pe": forward_pe,
            "peg": peg,
            "ps": ps,
            "pb": pb,
            "ev_ebitda": ev_ebitda,
            "ev_sales": ev_sales,
            # Profitability
            "gross_margin": gross_margin,
            "op_margin": op_margin,
            "net_margin": net_margin,
            "roe": roe,
            "roa": roa,
            # Balance sheet
            "debt_equity": debt_equity,
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "cash_per_share": cash_per_share,
            "total_debt": total_debt,
            "total_cash": total_cash,
            # Cash flow
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            # Growth
            "revenue_growth_yoy": revenue_growth,
            "earnings_growth_yoy": earnings_growth,
            "earnings_quarterly_growth": earnings_quarterly_growth,
            # Capital returns
            "shares_outstanding": shares_outstanding,
            "shares_float": shares_float,
            "dividend_yield": dividend_yield,
            "payout_ratio": payout_ratio,
            # Insider / institutional
            "insider_ownership": insider_ownership,
            "institutional_ownership": institutional_ownership,
            "short_ratio": short_ratio,
            "short_percent_float": short_percent,
            # Market data
            "week_52_high": week_52_high,
            "week_52_low": week_52_low,
            "week_52_position_pct": week_52_position,
            "avg_volume": avg_volume,
            "avg_volume_10d": avg_volume_10d,
            # Analyst
            "analyst_target": analyst_target,
            "analyst_recommendation": recommendation,
            # Earnings calendar
            "next_earnings_date": next_earnings_date,
            "next_earnings_days": next_earnings_days,
            # New signals
            "altman_z_score": altman_z_score,
            "relative_strength_6m": relative_strength_6m,
            "news_headlines": news_headlines,
            # Metadata
            "fetch_error": None,
            "cached": False,
        }

        cache[symbol] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        _save_cache(cache)
        return data

    except Exception as exc:
        error_data = {
            "symbol": symbol,
            "name": symbol,
            "sector": "Unknown",
            "industry": "Unknown",
            "fetch_error": str(exc),
            "cached": False,
        }
        return error_data


def fetch_multiple(tickers: list[str], delay_seconds: float = 1.0) -> dict[str, dict]:
    """
    Fetches fundamentals for a list of tickers with a polite delay between requests.
    Returns a dict keyed by ticker symbol.
    """
    spy_return_6m = get_spy_return_6m()
    results = {}
    for ticker in tickers:
        results[ticker.upper()] = fetch_fundamentals(ticker, spy_return_6m=spy_return_6m)
        time.sleep(delay_seconds)
    return results
