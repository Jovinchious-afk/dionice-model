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

import pandas as pd
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


MONTHS_HR = ["siječanj", "veljača", "ožujak", "travanj", "svibanj", "lipanj",
             "srpanj", "kolovoz", "rujan", "listopad", "studeni", "prosinac"]


def _round(value, digits: int = 4):
    return round(value, digits) if value is not None else None


def _quarter_yoy(df, label: str) -> float | None:
    """Latest quarter vs the same quarter a year earlier (yfinance columns are newest-first)."""
    try:
        if df is None or df.empty or label not in df.index:
            return None
        row = df.loc[label].dropna()
        if len(row) < 2:
            return None
        latest_date, latest = row.index[0], float(row.iloc[0])
        for date, value in row.iloc[1:].items():
            if 330 <= (latest_date - date).days <= 400:
                prior = float(value)
                return (latest - prior) / abs(prior) if prior else None
    except Exception:
        pass
    return None


def _quarterly_op_margins(qfin) -> list[float]:
    """Operating margin per quarter in %, newest first, up to 4 quarters."""
    try:
        if qfin is None or qfin.empty or "Operating Income" not in qfin.index or "Total Revenue" not in qfin.index:
            return []
        margins = []
        for col in qfin.columns:
            op, rev = qfin.loc["Operating Income", col], qfin.loc["Total Revenue", col]
            if op != op or rev != rev or not rev:
                continue
            margins.append(round(float(op) / float(rev) * 100, 1))
            if len(margins) == 4:
                break
        return margins
    except Exception:
        return []


def _insider_counts_24m(stock) -> tuple[int | None, int | None, int | None]:
    """
    Open-market insider purchases, sales, and rows Yahoo lists without any
    description (so they cannot be classified) over ~2 years — about as far back
    as Yahoo's list goes.
    """
    try:
        tx = stock.insider_transactions
        if tx is None or tx.empty or "Text" not in tx.columns:
            return None, None, None
        if "Start Date" in tx.columns:
            dates = pd.to_datetime(tx["Start Date"], errors="coerce")
            if getattr(dates.dt, "tz", None) is not None:
                dates = dates.dt.tz_localize(None)
            tx = tx[dates >= pd.Timestamp.now() - pd.Timedelta(days=730)]
        text = tx["Text"].fillna("").astype(str).str.strip()
        buys = int(text.str.contains("Purchase", case=False).sum())
        sells = int(text.str.contains("Sale at price", case=False).sum())
        return buys, sells, int((text == "").sum())
    except Exception:
        return None, None, None


def _dividend_cut(stock) -> bool | None:
    """True if the latest dividend is >15% below the median of the four before it, or regular payments stopped."""
    try:
        divs = stock.dividends
        if divs is None or len(divs) < 5:
            return None
        idx = pd.DatetimeIndex(divs.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        divs = pd.Series(divs.values, index=idx).sort_index()
        now = pd.Timestamp.now()
        if divs[divs.index >= now - pd.Timedelta(days=400)].empty:
            # Paid regularly, then went silent: that is a cut to zero
            return True if len(divs[divs.index >= now - pd.Timedelta(days=800)]) >= 3 else None
        return bool(float(divs.iloc[-1]) < 0.85 * float(divs.iloc[-5:-1].median()))
    except Exception:
        return None


def _compute_checklist_metrics(stock, info: dict) -> dict:
    """The trend data behind the investor's checklist: inventories, share count, debt, margins, insiders."""
    try:
        qbs = stock.quarterly_balance_sheet
    except Exception:
        qbs = None
    try:
        qfin = stock.quarterly_financials
    except Exception:
        qfin = None

    margins = _quarterly_op_margins(qfin)
    buys, sells, unclassified = _insider_counts_24m(stock)
    first_trade_ms = _safe_get(info, "firstTradeDateMilliseconds")
    return {
        "inventory_growth_yoy": _round(_quarter_yoy(qbs, "Inventory")),
        "quarterly_revenue_growth_yoy": _round(_quarter_yoy(qfin, "Total Revenue")),
        "shares_change_yoy": _round(_quarter_yoy(qbs, "Ordinary Shares Number")),
        "debt_change_yoy": _round(_quarter_yoy(qbs, "Total Debt")),
        "op_margin_quarters_pct": margins or None,
        "op_margin_declining_3q": (margins[0] < margins[1] < margins[2] < margins[3]) if len(margins) >= 4 else None,
        "insider_buys_24m": buys,
        "insider_sells_24m": sells,
        "insider_unclassified_24m": unclassified,
        "dividend_cut": _dividend_cut(stock),
        "analyst_count": _safe_get(info, "numberOfAnalystOpinions"),
        "years_listed": round((time.time() * 1000 - first_trade_ms) / (365.25 * 86_400_000), 1) if first_trade_ms else None,
    }


def _monthly_returns(closes: pd.Series) -> pd.Series:
    idx = pd.DatetimeIndex(closes.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    series = pd.Series(closes.values, index=idx.to_period("M"))
    series = series[~series.index.duplicated(keep="last")]
    return series.pct_change().dropna()


def get_spy_monthly_returns() -> pd.Series | None:
    """S&P 500 monthly returns over ~11 years, fetched once per batch for seasonality comparisons."""
    try:
        closes = yf.Ticker("SPY").history(period="11y", interval="1mo", auto_adjust=True)["Close"].dropna()
        return _monthly_returns(closes)
    except Exception:
        return None


def _compute_seasonality(stock, spy_monthly: pd.Series | None) -> dict | None:
    """
    How this calendar window (this month + next) went in past years, vs the S&P 500.
    A weak signal by design — shown as context, never used for scoring.
    """
    if spy_monthly is None or spy_monthly.empty:
        return None
    try:
        own = _monthly_returns(stock.history(period="11y", interval="1mo", auto_adjust=True)["Close"].dropna())
    except Exception:
        return None

    today = datetime.now(timezone.utc)
    first = pd.Period(year=today.year, month=today.month, freq="M")
    excess = []
    for years_back in range(1, 12):
        months = [first - 12 * years_back, first - 12 * years_back + 1]
        if any(m not in own.index or m not in spy_monthly.index for m in months):
            continue
        own_r = (1 + own[months[0]]) * (1 + own[months[1]]) - 1
        spy_r = (1 + spy_monthly[months[0]]) * (1 + spy_monthly[months[1]]) - 1
        excess.append(float(own_r - spy_r) * 100)
    if len(excess) < 5:
        return None

    s = pd.Series(excess)
    return {
        "window": f"{MONTHS_HR[first.month - 1]}–{MONTHS_HR[(first + 1).month - 1]}",
        "years": len(excess),
        "median_excess_pp": round(float(s.median()), 1),
        "beat_spy_years": int((s > 0).sum()),
        "best_excess_pp": round(float(s.max()), 1),
        "worst_excess_pp": round(float(s.min()), 1),
    }


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


def fetch_fundamentals(
    ticker: str,
    spy_return_6m: float | None = None,
    spy_monthly: pd.Series | None = None,
) -> dict:
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

        # --- The investor's checklist trends and calendar seasonality ---
        checklist_metrics = _compute_checklist_metrics(stock, info)
        seasonality = _compute_seasonality(stock, spy_monthly)

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
            "debt_equity": debt_equity,  # yfinance percent: 49.0 means 0.49x (used by scorer thresholds)
            "debt_to_equity_x": round(debt_equity / 100, 3) if debt_equity is not None else None,
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
            "seasonality": seasonality,
            **checklist_metrics,
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
    spy_monthly = get_spy_monthly_returns()
    results = {}
    for ticker in tickers:
        results[ticker.upper()] = fetch_fundamentals(ticker, spy_return_6m=spy_return_6m, spy_monthly=spy_monthly)
        time.sleep(delay_seconds)
    return results
