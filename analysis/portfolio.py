"""
Aggregates the transactions log into current holdings — shared by the weekly
newsletter, the monthly report and the Streamlit app so all three agree.

Average-cost method: a SELL removes cost in proportion to the shares sold and a
full exit resets cost to zero, so a later re-entry starts from its own price.
(Before this, the weekly run carried PTC's first lot into the second one and
told the model "PTC @ avg $172.36" when the real entry was $135.)

Every listed position trades in USD, so transactions entered in EUR are
converted at the EUR/USD close of their trade date.
"""

from datetime import datetime, timedelta

import yfinance as yf

FALLBACK_EUR_USD = 1.16  # only used when Yahoo is unreachable
_fx_cache: dict[str, float] = {}


def eur_usd_on(date_str: str | None = None) -> float:
    """EUR/USD close on or just before the given date (latest close when no date)."""
    day = (date_str or "")[:10]
    if day in _fx_cache:
        return _fx_cache[day]

    rate = None
    try:
        fx = yf.Ticker("EURUSD=X")
        if day:
            d = datetime.strptime(day, "%Y-%m-%d")
            closes = fx.history(
                start=(d - timedelta(days=7)).strftime("%Y-%m-%d"),
                end=(d + timedelta(days=1)).strftime("%Y-%m-%d"),
            )["Close"].dropna()
        else:
            closes = fx.history(period="5d")["Close"].dropna()
        if len(closes):
            rate = float(closes.iloc[-1])
    except Exception as exc:
        print(f"[portfolio] EUR/USD fetch failed for {day or 'today'}: {exc}")

    _fx_cache[day] = rate or FALLBACK_EUR_USD
    return _fx_cache[day]


def usd_to_eur_now() -> float:
    return 1 / eur_usd_on(None)


def compute_holdings(rows: list[dict]) -> dict[str, dict]:
    """
    Returns {symbol: {symbol, company_name, shares, cost_usd, avg_cost_usd,
    realized_pnl_usd}} for every symbol ever traded (shares may be 0).
    """
    holdings: dict[str, dict] = {}

    for row in sorted(rows, key=lambda r: str(r.get("trade_date") or "")):
        sym = row["symbol"]
        h = holdings.setdefault(sym, {
            "symbol": sym,
            "company_name": row.get("company_name") or sym,
            "shares": 0.0,
            "cost_usd": 0.0,
            "realized_pnl_usd": 0.0,
        })

        shares = float(row.get("shares") or 0)
        price = float(row.get("price_per_share") or 0)
        if str(row.get("currency") or "USD").upper() == "EUR":
            price *= eur_usd_on(str(row.get("trade_date") or ""))

        if row.get("action") == "BUY":
            h["shares"] += shares
            h["cost_usd"] += shares * price
        elif row.get("action") == "SELL":
            held = h["shares"]
            if held <= 0:
                continue
            sold = min(shares, held)
            avg = h["cost_usd"] / held
            h["realized_pnl_usd"] += sold * (price - avg)
            h["shares"] = held - sold
            if h["shares"] <= 1e-9:
                h["shares"], h["cost_usd"] = 0.0, 0.0
            else:
                h["cost_usd"] = avg * h["shares"]

    for h in holdings.values():
        h["avg_cost_usd"] = h["cost_usd"] / h["shares"] if h["shares"] > 0 else 0.0
    return holdings
